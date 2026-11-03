"""One entry decision per real new message, isolated from business preparation."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from tawg_bot.ai_router import AiRouteRejected, ContextualAiRouter, RouteDecision
from tawg_bot.bot_router import BotRouter, fallback_route
from tawg_bot.claude_cli import ClaudeCliError
from tawg_bot.conversation_context import (
    ConversationContextBuilder,
    ConversationContextRejected,
)
from tawg_bot.feature_flags import ENTRY_ROUTER_VERSION
from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    RouteContextScope,
    SourceRecord,
    TriggerKind,
)
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.query import SourceQuery
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class EntryAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["route", "route_entry"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...

ENTRY_CANDIDATE_KINDS = frozenset(
    {
        TriggerKind.MENTION,
        TriggerKind.REPLY_TO_BOT,
        TriggerKind.GREETING_CANDIDATE,
        TriggerKind.MESSAGE_CANDIDATE,
        TriggerKind.MEMBER_WELCOME,
        TriggerKind.MEMBER_INTRODUCTION,
    }
)


@dataclass(frozen=True, slots=True)
class EntryReport:
    candidates: int
    decided: int
    ai_calls: int
    ignored: int
    failed: int
    privacy_rejected: int


class EntryDecider:
    """Classify each real new message exactly once, before business preparation.

    ``decide`` is only called from the two intake paths. It never runs from a
    maintenance phase, which is what guarantees zero entry AI calls on an empty tick.
    There is deliberately no batch cap and no TTL: every eligible message is processed.
    """

    _BUDGET_USD = "0.30"
    _TIMEOUT_SECONDS = 60.0
    _CONTEXT_MAX_CHARS = 24_000
    _CONTEXT_MAX_PRIOR_RECORDS = 100

    def __init__(
        self,
        root: Path,
        *,
        ai: EntryAi,
        bot_username: str,
        now: datetime,
    ) -> None:
        self.root = root.resolve()
        self.ai = ai
        self.bot_username = bot_username
        self.now = now
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def decide(self, job_ids: Sequence[str]) -> EntryReport:
        jobs = self._load_jobs()
        selected = [
            jobs[job_id]
            for job_id in job_ids
            if job_id in jobs and jobs[job_id].trigger_kind in ENTRY_CANDIDATE_KINDS
        ]
        groups: dict[str, list[PendingBotJob]] = defaultdict(list)
        for job in selected:
            if job.classified_route is not None and job.routed_at is not None:
                continue
            groups[job.trigger_record_id].append(job)

        records = {record.record_id: record for record in SourceQuery(self.root).records()}
        updates: dict[str, PendingBotJob] = {}
        ai_calls = 0
        ignored = 0
        failed = 0
        privacy_rejected = 0

        for trigger_record_id in sorted(groups):
            group = groups[trigger_record_id]
            representative = _representative(group)
            trigger = records.get(trigger_record_id)
            if trigger is None:
                for job in group:
                    updates[job.job_id] = _cancelled(job, "entry_trigger_missing")
                failed += 1
                continue
            try:
                context = ConversationContextBuilder(self.privacy).build(
                    trigger=trigger,
                    records=records.values(),
                    message_thread_id=representative.message_thread_id,
                    max_chars=self._CONTEXT_MAX_CHARS,
                    max_prior_records=self._CONTEXT_MAX_PRIOR_RECORDS,
                    trigger_kind=representative.trigger_kind,
                )
            except ConversationContextRejected:
                for job in group:
                    updates[job.job_id] = _rejected(job, trigger, self.now)
                privacy_rejected += 1
                continue

            safe_code: str | None = None
            try:
                decision = await ContextualAiRouter(
                    self.ai, entry_mode=True
                ).classify(
                    context,
                    operation_id=f"{representative.job_id}:entry",
                    max_budget_usd=self._BUDGET_USD,
                    timeout_seconds=self._TIMEOUT_SECONDS,
                )
                ai_calls += 1
                scope = decision.context_scope
                context_sha256 = decision.context_sha256
                artifact = decision.artifact
                route = self._authorize(representative, decision, trigger)
            except (AiRouteRejected, ClaudeCliError):
                safe_code = "entry_model_failed"
                route, scope = fallback_route(
                    trigger_kind=representative.trigger_kind,
                    trigger_text=trigger.text_original,
                    bot_username=self.bot_username,
                )
                context_sha256 = context.sha256
                artifact = None
                failed += 1

            for job in group:
                job_status = _status_for(job.trigger_kind, route)
                if job_status is JobStatus.IGNORED:
                    ignored += 1
                payload = {
                    "classified_route": route,
                    "router_context_scope": scope,
                    "router_context_sha256": context_sha256,
                    "router_version": ENTRY_ROUTER_VERSION,
                    "routed_at": self.now,
                    "verification_artifact": (
                        None if route is BotRoute.IGNORE else artifact
                    ),
                    "safe_error_code": safe_code,
                }
                if job_status is not None:
                    payload["status"] = job_status
                updates[job.job_id] = job.model_copy(update=payload)

        if updates:
            self._publish(updates)
        report = EntryReport(
            candidates=len(selected),
            decided=len(groups),
            ai_calls=ai_calls,
            ignored=ignored,
            failed=failed,
            privacy_rejected=privacy_rejected,
        )
        print(
            "tawg_event=entry_decisions "
            f"candidates={report.candidates} decided={report.decided} "
            f"ai_calls={report.ai_calls} ignored={report.ignored} "
            f"failed={report.failed} privacy_rejected={report.privacy_rejected}",
            flush=True,
        )
        return report

    async def migrate_legacy(self) -> EntryReport:
        """One-time operator migration; never called from a scheduled tick."""
        jobs = self._load_jobs()
        job_ids = [
            job_id
            for job_id, job in jobs.items()
            if job.trigger_kind in ENTRY_CANDIDATE_KINDS
            and job.classified_route is None
            and job.routed_at is None
            and job.status in {JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.READY}
        ]
        return await self.decide(job_ids)

    def _authorize(
        self,
        job: PendingBotJob,
        decision: RouteDecision,
        trigger: SourceRecord,
    ) -> BotRoute:
        if job.trigger_kind in {
            TriggerKind.MEMBER_WELCOME,
            TriggerKind.MEMBER_INTRODUCTION,
        }:
            return decision.route
        if job.trigger_kind in {
            TriggerKind.GREETING_CANDIDATE,
            TriggerKind.MESSAGE_CANDIDATE,
        }:
            # Ordinary group messages have a silent IGNORE exit. A reply is allowed
            # only for a needs-reply category or an AI-structured direct-to-bot claim.
            if decision.decision == "ignore":
                return BotRoute.IGNORE
            if decision.route is BotRoute.KNOWLEDGE_QUESTION:
                return BotRoute.KNOWLEDGE_QUESTION
            if decision.addressed_to_bot and decision.route in {
                BotRoute.COORDINATION,
                BotRoute.REFUSE,
            }:
                return decision.route
            return BotRoute.IGNORE
        route = decision.route if decision.decision == "reply" else BotRoute.IGNORE
        return BotRouter(self.bot_username).authorize_ai_route(
            route,
            job.trigger_kind,
            trigger.text_original,
        )

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        path = self.root / "data/state/pending-bot-jobs.json"
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        jobs = [PendingBotJob.model_validate(item) for item in raw]
        return {job.job_id: job for job in jobs}

    def _publish(self, updates: dict[str, PendingBotJob]) -> None:
        # Re-read immediately before writing: the file is the whole job set, so replacing
        # it with only this batch would delete unrelated jobs.
        jobs = self._load_jobs()
        jobs.update(updates)
        uow = RepositoryUnitOfWork(self.root, operation_id=f"entry-decisions-{uuid4()}")
        uow.register_external_evidence(())
        uow.stage_json(
            "data/state/pending-bot-jobs.json",
            [jobs[job_id].persistence_payload() for job_id in sorted(jobs)],
        )
        uow.publish()


def _representative(jobs: list[PendingBotJob]) -> PendingBotJob:
    preferred = {
        TriggerKind.MESSAGE_CANDIDATE,
        TriggerKind.MENTION,
        TriggerKind.REPLY_TO_BOT,
        TriggerKind.GREETING_CANDIDATE,
    }
    for job in sorted(jobs, key=lambda item: item.job_id):
        if job.trigger_kind in preferred:
            return job
    for job in sorted(jobs, key=lambda item: item.job_id):
        if job.trigger_kind is TriggerKind.MEMBER_WELCOME:
            return job
    return sorted(jobs, key=lambda item: item.job_id)[0]


def _status_for(trigger_kind: TriggerKind, route: BotRoute) -> JobStatus | None:
    if trigger_kind in {
        TriggerKind.MEMBER_WELCOME,
        TriggerKind.MEMBER_INTRODUCTION,
    }:
        return None
    if route is BotRoute.IGNORE:
        return JobStatus.IGNORED
    return None


def _rejected(job: PendingBotJob, trigger: SourceRecord, now: datetime) -> PendingBotJob:
    if job.trigger_kind in {
        TriggerKind.MESSAGE_CANDIDATE,
        TriggerKind.GREETING_CANDIDATE,
    }:
        return job.model_copy(
            update={
                "status": JobStatus.IGNORED,
                "safe_error_code": "privacy_rejected",
                "classified_route": BotRoute.IGNORE,
                "router_context_scope": RouteContextScope.CONVERSATION,
                "router_context_sha256": trigger.content_sha256,
                "router_version": ENTRY_ROUTER_VERSION,
                "routed_at": now,
            }
        )
    return job.model_copy(
        update={
            "status": JobStatus.CANCELLED,
            "safe_error_code": "privacy_rejected",
            "classified_route": None,
            "router_context_scope": None,
            "router_context_sha256": None,
            "router_version": None,
            "routed_at": None,
        }
    )


def _cancelled(job: PendingBotJob, code: str) -> PendingBotJob:
    return job.model_copy(
        update={
            "status": JobStatus.CANCELLED,
            "safe_error_code": code,
        }
    )
