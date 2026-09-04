"""Production composition for the scheduled repository-backed bot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import httpx
from pydantic import ValidationError

from tawg_bot.bot_identity import configured_bot_id
from tawg_bot.bot_router import (
    BotReplyService,
    PreparedReply,
    ReplyRejected,
    ReplyRepairReconciler,
)
from tawg_bot.busy_question import (
    AskQuestionResult,
    BusyQuestionConfig,
    BusyQuestionService,
)
from tawg_bot.claude_cli import ClaudeCli, ClaudeCliError
from tawg_bot.daily import (
    DailyReadiness,
    DailyRejected,
    DailyService,
    DailyWindow,
    PreparedDaily,
)
from tawg_bot.daily_evidence import (
    DailyEvidenceCollector,
    GitHubActivityRecords,
    MagiciansActivityRecords,
)
from tawg_bot.delivery import (
    DeliveryAmbiguous,
    DeliveryCheckpoint,
    DeliveryFailed,
    DeliveryService,
)
from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.evidence_fetch import RestrictedEvidenceFetcher
from tawg_bot.github_announcements import (
    GitHubAnnouncementBatch,
    GitHubAnnouncementScanner,
    render_announcement,
)
from tawg_bot.github_source import GitHubHttpClient, GitHubSourceError
from tawg_bot.http import SafeJsonHttpClient
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_refresh import KnowledgeRefresh, RefreshResult
from tawg_bot.live_evidence import EvidencePack, LiveEvidenceService
from tawg_bot.magicians_source import MagiciansHttpClient
from tawg_bot.models import (
    DeliveryAttempt,
    DeliveryStatus,
    JobStatus,
    PendingBotJob,
    TriggerKind,
)
from tawg_bot.persist_mode import PersistMode, configured_persist_mode
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.repository_session import RepositoryConflict
from tawg_bot.scan_targets import ScanTargetStore, ScanTargetVerifier
from tawg_bot.scheduler import IntakePolicy, Scheduler
from tawg_bot.scoped_scanner import ScopedScanResult, ScopedSourceScanner
from tawg_bot.source_registry import SourceRegistry
from tawg_bot.telegram_api import TelegramApi
from tawg_bot.telegram_intake import (
    MemberWelcomeReconciler,
    TelegramIntake,
    WebhookIntakeResult,
    ingest_envelopes,
)
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class RuntimeFailure(RuntimeError):
    """A safe production-composition failure."""


_SOURCE_RECHECK_INTERVAL = timedelta(hours=24)
_SOURCE_ERC_TIMEOUT_SECONDS = 60
_SOURCE_OPERATION_SECONDS = 45
_DAILY_EVIDENCE_TIMEOUT_SECONDS = 60
_DAILY_TIMEOUT_SECONDS = 900
_REPLY_TIMEOUT_SECONDS = 300
_REPLY_PHASE_BUDGET_SECONDS = 1_200
_BUSY_QUESTION_TIMEOUT_SECONDS = 60
_BUSY_QUESTION_BUDGET_USD = "0.05"
_PROCESSING_LEASE = timedelta(minutes=10)
_MAX_REPLIES_PER_TICK = 10
_TELEGRAM_GROUP_SLUG = "tawg"
_RICH_DAILY_PREVIEW_SOURCE_ID = "daily:2026-08-27T23:00:00Z"
_RICH_DAILY_PREVIEW_SOURCE_SHA256 = (
    "78bb59915ac2873a30dedb7416e039ade46b80ba602c4e20e85e5d842e507262"
)
_RICH_DAILY_PREVIEW_JOB_ID = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
_DAILY_REJECTION_CODES = {
    "invalid Daily model output": "daily_model_output_invalid",
    "Daily output changed the fixed UTC window": "daily_window_invalid",
    "Daily title must match the exact UTC window": "daily_title_invalid",
    "Daily title must match the Rich Markdown contract": "daily_title_invalid",
    "Daily title or UTC window is missing": "daily_title_invalid",
    "Daily UTC window must match the fixed window": "daily_window_invalid",
    "Daily output exceeds the Telegram budget": "daily_size_invalid",
    "Daily cannot fit in at most two Telegram messages": "daily_size_invalid",
    "Daily output failed privacy validation": "daily_privacy_invalid",
    "Daily output must be English": "daily_language_invalid",
    "Daily factual bullet lacks a valid citation": "daily_citation_invalid",
    "Daily highlight lacks a valid citation": "daily_citation_invalid",
    "Daily citation list contains duplicates": "daily_citation_invalid",
    "active Daily lacks a citation from its fixed window": "daily_citation_invalid",
    "Daily synthesis contains source-dependent detail": "daily_synthesis_invalid",
    "Daily What moved has an invalid direction structure": "daily_structure_invalid",
    "Daily concrete progress uses an invalid bullet marker": "daily_structure_invalid",
    "Daily concrete progress bullet uses an invalid Rich Markdown list marker": (
        "daily_structure_invalid"
    ),
    "active Daily lacks a complete What moved direction": "daily_structure_invalid",
    "Daily highlight has an invalid Rich Markdown shape": "daily_structure_invalid",
    "active Daily has an invalid highlight count": "daily_structure_invalid",
    "Daily Trusty's take has an invalid quote structure": "daily_structure_invalid",
    "Daily Trusty's take lacks Today's spark": "daily_structure_invalid",
    "Daily output has an unexpected top-level section": "daily_sections_invalid",
    "Daily output has content before Highlights": "daily_sections_invalid",
    "Daily output has required sections out of order": "daily_sections_invalid",
    "Daily text contains an unknown citation": "daily_citation_unknown",
    "Daily citation references unknown evidence": "daily_citation_unknown",
    "Daily output contains ranking or persona language": "daily_tone_invalid",
    "Daily output exceeds the emoji limit": "daily_tone_invalid",
    "Daily Trusty's take must end with an emoji": "daily_tone_invalid",
    "Daily must integrate Appreciation into What moved": "daily_tone_invalid",
    "Daily contributor lacks a confirmed Telegram mention": "daily_mention_invalid",
    "Daily contains an invalid Telegram mention": "daily_mention_invalid",
    "Daily citation has conflicting contributor mappings": "daily_mention_invalid",
    "quiet Daily invents source-backed progress": "daily_grounding_invalid",
    "quiet Daily has an invalid highlight": "daily_grounding_invalid",
    "Daily Trusty's take contains source-dependent detail": ("daily_grounding_invalid"),
    "quiet Daily must state that no source-backed progress landed": ("daily_grounding_invalid"),
    "Daily evidence falls outside the fixed UTC window": "daily_evidence_invalid",
    "priority context does not fit the configured budget": "daily_context_invalid",
    "invalid TAWG alias registry": "daily_alias_invalid",
    "invalid delivery state": "daily_state_invalid",
    "invalid Daily Telegram split policy": "daily_config_invalid",
    "invalid bot policy": "daily_config_invalid",
    "incomplete Daily bot policy": "daily_config_invalid",
    "Daily schema must be an object": "daily_config_invalid",
    "configuration must be a mapping": "daily_config_invalid",
}


def _daily_rejection_code(error: DailyRejected) -> str:
    reason = str(error)
    exact = _DAILY_REJECTION_CODES.get(reason)
    if exact is not None:
        return exact
    if reason.startswith("Daily output has an invalid required section:"):
        return "daily_sections_invalid"
    if reason.endswith(" is not fresh through the Daily cutoff"):
        return "daily_readiness_stale"
    if reason.startswith("context privacy rejection:"):
        return "daily_privacy_invalid"
    return "daily_validation_failed"


@dataclass(frozen=True, slots=True)
class SourceCheckSummary:
    erc_count: int
    evidence_count: int
    gap_count: int
    refresh_job_count: int
    persisted: bool


class ScriptCheckpoint(DeliveryCheckpoint):
    def __init__(self, script: Path) -> None:
        self.script = script.resolve()

    async def publish(self, operation_id: str, root: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(self.script),
            operation_id,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        del stdout, stderr
        if process.returncode == 75:
            raise RepositoryConflict
        if process.returncode != 0:
            raise RuntimeFailure("repository checkpoint failed")


class ProductionRuntime:
    def __init__(self, root: Path, *, checkpoint: DeliveryCheckpoint) -> None:
        self.root = root.resolve()
        self.checkpoint = checkpoint

    @classmethod
    def from_environment(cls, root: Path) -> ProductionRuntime:
        return cls(root, checkpoint=ScriptCheckpoint(root / "scripts/commit_operation.sh"))

    async def tick(self, now: datetime, *, observe_only: bool) -> None:
        await self._scheduled_tick(
            now,
            observe_only=observe_only,
            intake_policy=IntakePolicy.POLL,
        )

    async def maintenance_tick(self, now: datetime, *, observe_only: bool) -> None:
        await self._scheduled_tick(
            now,
            observe_only=observe_only,
            intake_policy=IntakePolicy.SKIP,
        )

    async def ingest_webhook_envelope(
        self,
        envelope: TelegramWebhookEnvelope,
        *,
        now: datetime,
    ) -> WebhookIntakeResult:
        _require_utc(now, "webhook ingestion time")
        bot_username = _configured_bot_username()
        bot_id = configured_bot_id()
        persist_mode = configured_persist_mode()
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(
                self.root,
                client=client,
                checkpoint=self.checkpoint,
                now=now,
                bot_id=bot_id,
                persist_mode=persist_mode,
            )
            result = ingest_envelopes(
                root=self.root,
                group_slug=_TELEGRAM_GROUP_SLUG,
                bot_username=bot_username,
                envelopes=(envelope,),
                now=now,
                telegram_chat_id=pipeline._chat_id(),
                bot_id=bot_id,
                persist_mode=persist_mode,
            )
            pipeline.telegram_synced_at = now
            await self.checkpoint.publish(f"telegram-webhook:{envelope.update_id}", self.root)
            await pipeline.publish_repository()
            await pipeline.telegram_delivery()
        return result

    async def _scheduled_tick(
        self,
        now: datetime,
        *,
        observe_only: bool,
        intake_policy: IntakePolicy,
    ) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(
                self.root,
                client=client,
                checkpoint=self.checkpoint,
                now=now,
                member_introductions_enabled=True,
            )
            result = await Scheduler(self.root, pipeline=pipeline).tick(
                now,
                observe_only=observe_only,
                intake_policy=intake_policy,
            )
        try:
            await self.checkpoint.publish(
                f"layer-success:{result.layer.name.casefold()}:{int(now.timestamp())}",
                self.root,
            )
        except RepositoryConflict:
            raise
        except Exception:
            _safe_log("final_checkpoint", "final_checkpoint_failed")
            raise RuntimeFailure("scheduled tick final checkpoint failed") from None
        if result.failed_phases:
            raise RuntimeFailure("scheduled tick completed with phase failures")
        if getattr(pipeline, "reply_failures", ()):
            raise RuntimeFailure("scheduled tick completed with reply job failures")

    async def check_sources(self, erc: int | None, *, observe_only: bool) -> SourceCheckSummary:
        now = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(self.root, client=client, checkpoint=self.checkpoint, now=now)
            summary = await pipeline.check_sources(
                now,
                erc_numbers=(erc,) if erc is not None else None,
                observe_only=observe_only,
            )
            if not observe_only:
                await pipeline.publish_repository()
        return summary

    async def refresh_knowledge(self, erc: int | None, *, dry_run: bool) -> RefreshResult:
        now = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=30) as client:
            if dry_run:
                with TemporaryDirectory(prefix="tawg-knowledge-dry-run-") as runtime_directory:
                    pipeline = _LivePipeline(
                        self.root,
                        client=client,
                        checkpoint=self.checkpoint,
                        now=now,
                        ai=ClaudeCli(root=self.root, runtime_root=Path(runtime_directory)),
                    )
                    return await pipeline.refresh_knowledge(
                        now,
                        erc_numbers=frozenset({erc}) if erc is not None else None,
                        dry_run=True,
                    )
            pipeline = _LivePipeline(self.root, client=client, checkpoint=self.checkpoint, now=now)
            if erc is not None:
                await pipeline.check_sources(now, erc_numbers=(erc,), observe_only=False)
            result = await pipeline.refresh_knowledge(
                now,
                erc_numbers=frozenset({erc}) if erc is not None else None,
                dry_run=False,
            )
            await pipeline.publish_repository()
        return result

    async def daily_dry_run(self, window_end: datetime) -> PreparedDaily | None:
        _require_utc(window_end, "Daily dry-run end")
        now = datetime.now(UTC)
        if now < window_end:
            raise RuntimeFailure("Daily dry-run window cannot end in the future")
        with TemporaryDirectory(prefix="tawg-daily-dry-run-") as runtime_directory:
            ai = ClaudeCli(root=self.root, runtime_root=Path(runtime_directory))
            async with httpx.AsyncClient(timeout=30) as client:
                pipeline = _LivePipeline(
                    self.root,
                    client=client,
                    checkpoint=self.checkpoint,
                    now=now,
                    ai=ai,
                )
                window = DailyWindow(
                    start=window_end - timedelta(days=1),
                    end=window_end,
                    window_id=f"daily:{window_end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                )
                return await pipeline.prepare_daily(window, dry_run=True)


class _LivePipeline:
    def __init__(
        self,
        root: Path,
        *,
        client: httpx.AsyncClient,
        checkpoint: DeliveryCheckpoint,
        now: datetime,
        ai: ClaudeCli | None = None,
        member_introductions_enabled: bool = False,
        bot_id: int | None = None,
        persist_mode: PersistMode = PersistMode.FULL,
    ) -> None:
        self.root = root.resolve()
        self.client = client
        self.checkpoint = checkpoint
        self.now = now
        self.ai = ai or ClaudeCli(root=self.root)
        self.registry = SourceRegistry.from_yaml(self.root / "knowledge/meta/sources.yml")
        self.live_evidence = LiveEvidenceService(
            root=self.root,
            registry=self.registry,
            fetcher=RestrictedEvidenceFetcher(client=client),
            operation_seconds=_SOURCE_OPERATION_SECONDS,
        )
        self.knowledge_state = KnowledgeStateStore(self.root, registry=self.registry)
        # VERIFICATION route (PR #9) shipped BotReplyService's invinoveritas_client/
        # invinoveritas_api_key parameters but never actually constructed or passed them
        # here -- real gap found live 2026-09-01 (a genuine @trustless_ai_devbot verify
        # request came back "invinoveritas verification is not configured" because the
        # route defaults to None regardless of what's set on Modal). Wire it the same way
        # every other external client in this class already is: share the one httpx.AsyncClient,
        # read the credential from the environment, never hardcode it.
        self.invinoveritas_client = SafeJsonHttpClient(client)
        self.invinoveritas_api_key = os.environ.get("TAWG_INVINOVERITAS_API_KEY")
        try:
            registration_github = GitHubHttpClient.from_env(client=client)
        except GitHubSourceError:
            registration_github = None
        registration_topics = MagiciansHttpClient(
            base_url="https://ethereum-magicians.org",
            client=client,
        )
        self.scan_target_verifier = ScanTargetVerifier(
            topic_client=registration_topics,
            github_client=registration_github,
        )
        self.scoped_scanner = ScopedSourceScanner(
            self.root,
            github_client=registration_github,
            topic_client=registration_topics,
        )
        self.github_announcements = GitHubAnnouncementScanner(
            self.root,
            client=registration_github,
        )
        self.telegram_synced_at: datetime | None = None
        self.source_checked_at: datetime | None = None
        self.live_evidence_collected_at: datetime | None = None
        self.knowledge_refreshed_at: datetime | None = None
        self.knowledge_attempted = False
        self.daily_attempted = False
        self.prepared_daily: PreparedDaily | None = None
        self.prepared_replies: list[PreparedReply] = []
        self.reply_failures: list[str] = []
        self.member_introductions_enabled = member_introductions_enabled
        self.bot_id = bot_id
        self.persist_mode = persist_mode

    async def telegram_intake(self, now: datetime) -> None:
        api = TelegramApi.from_env(client=self.client)
        intake = TelegramIntake.from_env(root=self.root, api=api)
        await intake.collect(now)
        self.telegram_synced_at = now
        await self.checkpoint.publish(f"telegram-intake:{int(now.timestamp())}", self.root)

    async def source_check(self, now: datetime) -> None:
        try:

            async def scan_all() -> tuple[ScopedScanResult, GitHubAnnouncementBatch]:
                scoped = await self.scoped_scanner.scan(
                    since=now - _SOURCE_RECHECK_INTERVAL,
                    now=now,
                )
                if scoped.failed_sources:
                    raise RuntimeFailure("source check incomplete")
                announcements = await self.github_announcements.scan(now=now)
                return scoped, announcements

            result, announcement_batch = await asyncio.wait_for(
                scan_all(),
                timeout=_SOURCE_ERC_TIMEOUT_SECONDS,
            )
            uow = RepositoryUnitOfWork(
                self.root,
                operation_id=f"scoped-source-check:{int(now.timestamp())}",
            )
            uow.register_external_evidence(())
            self.scoped_scanner.stage(result, uow)
            self.github_announcements.stage(announcement_batch, uow)
            uow.publish()
            await self.checkpoint.publish(
                f"scoped-source-check:{int(now.timestamp())}",
                self.root,
            )
        except RepositoryConflict:
            raise
        except Exception:
            _safe_log("source_check", "source_check_failed")
            raise RuntimeFailure("source check incomplete") from None
        self.source_checked_at = now

    async def check_sources(
        self,
        now: datetime,
        *,
        erc_numbers: tuple[int, ...] | None = None,
        observe_only: bool,
    ) -> SourceCheckSummary:
        _require_utc(now, "source check time")
        numbers = self.registry.erc_numbers() if erc_numbers is None else erc_numbers
        packs: list[EvidencePack] = []
        for erc_number in numbers:
            packs.append(
                await self.live_evidence.build(
                    ErcQuery(
                        erc_numbers=(erc_number,),
                        intent=ErcIntent.IMPLEMENTATION,
                    ),
                    now=now,
                )
            )
        if not observe_only and packs:
            uow = RepositoryUnitOfWork(
                self.root, operation_id=f"source-check:{int(now.timestamp())}"
            )
            uow.register_external_evidence(item.text for pack in packs for item in pack.evidence)
            self.knowledge_state.stage_compilation_outcome(
                uow,
                tuple(pack.for_persistence() for pack in packs),
                frozenset(),
                now=now,
            )
            uow.publish()
            self._reload_registry()
        self.source_checked_at = now
        return SourceCheckSummary(
            erc_count=len(numbers),
            evidence_count=sum(len(pack.evidence) for pack in packs),
            gap_count=sum(len(pack.missing_required) for pack in packs),
            refresh_job_count=sum(len(pack.source_changes) for pack in packs),
            persisted=bool(packs) and not observe_only,
        )

    async def knowledge_refresh(self, cutoff: datetime) -> None:
        _require_utc(cutoff, "knowledge refresh cutoff")
        self.knowledge_refreshed_at = self.now

    async def refresh_knowledge(
        self,
        cutoff: datetime,
        *,
        erc_numbers: frozenset[int] | None = None,
        dry_run: bool = False,
    ) -> RefreshResult:
        result = await KnowledgeRefresh(
            self.root,
            ai=self.ai,
            live_evidence=self.live_evidence,
            registry=self.registry,
        ).run(
            cutoff=cutoff,
            operation_id=f"knowledge-refresh-{cutoff.strftime('%Y%m%dt%H%M%Sz')}",
            erc_numbers=erc_numbers,
            dry_run=dry_run,
        )
        if not dry_run:
            self.knowledge_refreshed_at = self.now
        return result

    async def validate(self) -> None:
        report = VaultLinter(self.root).lint(now=self.now)
        if report.error_count:
            raise RuntimeFailure("vault validation failed")

    async def daily_prepare(self, window_id: str) -> None:
        window = DailyWindow.for_due_run(self.now)
        if window.window_id != window_id:
            raise RuntimeFailure("scheduler supplied an inconsistent Daily window")
        self.daily_attempted = True
        try:
            prepared = await self.prepare_daily(window, dry_run=False)
        except PersistenceRejected:
            self.prepared_daily = None
            _safe_log("daily_persistence", "daily_persistence_rejected")
            raise RuntimeFailure("Daily persistence failed") from None
        except DailyRejected as error:
            self.prepared_daily = None
            _safe_log("daily_validation", _daily_rejection_code(error))
            raise RuntimeFailure("Daily validation failed") from None
        except ClaudeCliError as error:
            self.prepared_daily = None
            code = (
                "daily_model_timeout"
                if str(error) == "Claude Code exceeded its time limit"
                else "daily_model_failed"
            )
            _safe_log("daily_model", code)
            raise RuntimeFailure("Daily model failed") from None
        except Exception:
            self.prepared_daily = None
            raise
        if prepared is None:
            return
        try:
            await self.checkpoint.publish(
                f"daily-prepared:{int(window.end.timestamp())}", self.root
            )
        except RepositoryConflict:
            raise
        except Exception:
            self.prepared_daily = None
            _safe_log("daily_prepare", "daily_checkpoint_failed")
            raise RuntimeFailure("Daily checkpoint incomplete") from None

    async def prepare_daily(self, window: DailyWindow, *, dry_run: bool) -> PreparedDaily | None:
        delivery_job_id = (
            window.window_id
            if dry_run
            else _authorized_rich_daily_preview_job_id(self.root, window)
        )
        if delivery_job_id != window.window_id:
            preview_attempt = next(
                (
                    attempt
                    for attempt in _load_delivery_attempts(self.root)
                    if attempt.delivery_id == delivery_job_id
                ),
                None,
            )
            if preview_attempt is not None and preview_attempt.status is DeliveryStatus.DELIVERED:
                self.prepared_daily = None
                return None
            recovered = _recover_rich_daily_preview(self.root, delivery_job_id=delivery_job_id)
            if recovered is not None:
                self.prepared_daily = recovered
                return recovered
        ready_at = self.now
        evidence = await DailyEvidenceCollector(
            self.root,
            github=GitHubActivityRecords(
                self.root,
                client=self.client,
                scan_registry=ScanTargetStore(self.root).load(),
            ),
            magicians=MagiciansActivityRecords(
                self.root,
                client=self.client,
                registry=ScanTargetStore(self.root).load(),
            ),
            timeout_seconds=_DAILY_EVIDENCE_TIMEOUT_SECONDS,
        ).collect(window, now=self.now)
        self.live_evidence_collected_at = self.now
        readiness = DailyReadiness(
            telegram_synced_at=self.telegram_synced_at or ready_at,
            live_evidence_collected_at=self.live_evidence_collected_at,
            knowledge_refreshed_at=self.knowledge_refreshed_at or ready_at,
        )
        prepared = await DailyService(
            self.root,
            ai=self.ai,
            timeout_seconds=_DAILY_TIMEOUT_SECONDS,
        ).prepare(window, readiness=readiness, evidence=evidence)
        if prepared is None:
            self.prepared_daily = None
            return None
        if delivery_job_id != window.window_id:
            prepared = replace(prepared, window_id=delivery_job_id)
        if dry_run:
            self.prepared_daily = prepared
            return prepared
        artifact = {
            "schema": "tawg.prepared-daily.v1",
            "dry_run": dry_run,
            "window_id": prepared.window_id,
            "telegram_text": prepared.telegram_text,
            "citations": list(prepared.citations),
            "quiet_day": prepared.quiet_day,
            "prepared_at": self.now.isoformat().replace("+00:00", "Z"),
        }
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"daily-prepared:{int(window.end.timestamp())}"
        )
        uow.register_external_evidence(
            item.text for item in evidence if item.source_kind != "telegram"
        )
        cited_external_locators = tuple(
            item.citation
            for item in evidence
            if item.source_kind != "telegram" and item.citation in prepared.citations
        )
        if cited_external_locators:
            uow.register_trusted_source_locators(cited_external_locators)
        uow.stage_json("data/state/prepared-daily.json", artifact)
        uow.publish()
        self.prepared_daily = prepared
        return prepared

    async def publish_repository(self) -> None:
        await self._prepare_pending_replies()
        await self.checkpoint.publish(f"prepared:{int(self.now.timestamp())}", self.root)

    async def telegram_delivery(self) -> None:
        api = TelegramApi.from_env(client=self.client)
        chat_id = self._chat_id()
        delivery = DeliveryService(
            self.root,
            api=api,
            chat_id=chat_id,
            checkpoint=self.checkpoint,
            bot_id=self.bot_id,
            persist_mode=self.persist_mode,
        )
        pending_announcements = self.github_announcements.pending()
        announcement_topic_id: int | None = None
        if pending_announcements:
            try:
                announcement_topic_id = self._github_announcement_topic_id()
            except RuntimeFailure:
                _safe_log(
                    "github_announcement_delivery",
                    "invalid_topic_configuration",
                )
        for event in pending_announcements if announcement_topic_id is not None else ():
            try:
                await delivery.deliver(
                    job_id=event.event_id,
                    text=render_announcement(event),
                    reply_to_message_id=None,
                    message_thread_id=announcement_topic_id,
                    now=self.now,
                )
            except (DeliveryAmbiguous, DeliveryFailed):
                continue
            self.github_announcements.acknowledge(event.event_id)
            digest = event.event_id.rsplit(":", 1)[-1]
            await self.checkpoint.publish(
                f"github-announcement-ack:{digest}",
                self.root,
            )
        for reply in self.prepared_replies:
            try:
                await delivery.deliver(
                    job_id=reply.job_id,
                    text=reply.reply_text,
                    reply_to_message_id=reply.reply_to_message_id,
                    message_thread_id=reply.message_thread_id,
                    attachments=reply.attachments,
                    now=self.now,
                )
            except (DeliveryAmbiguous, DeliveryFailed):
                continue
        if self.prepared_daily is not None:
            await delivery.deliver(
                job_id=self.prepared_daily.window_id,
                text=self.prepared_daily.telegram_text,
                reply_to_message_id=None,
                message_thread_id=None,
                now=self.now,
            )

    async def maybe_ask_busy_question(self, now: datetime) -> None:
        """Ask another bot a short "what happened" question when the group got busy.

        Runs the deterministic message-count / cooldown check first, then (only when it
        decides to ask) makes one bounded AI call for a playful question and sends it. The
        trigger state is staged only after a successful delivery, so a transient Telegram
        failure retries on the next tick instead of silently skipping the window.
        """
        _require_utc(now, "busy question time")
        config = BusyQuestionConfig.from_env()
        service = BusyQuestionService(self.root, config=config)
        decision = service.decide(now)
        if not decision.should_ask:
            return
        context_pack = json.dumps(
            {
                "context_schema": "tawg.ask-question-context.v1",
                "target": config.target,
                "recent_message_count": decision.recent_count,
                "window_seconds": config.window_seconds,
            }
        )
        raw = await self.ai.run(
            job_type="ask_question",
            context_pack=context_pack,
            operation_id=f"busy-question:{int(now.timestamp())}",
            max_budget_usd=_BUSY_QUESTION_BUDGET_USD,
            timeout_seconds=_BUSY_QUESTION_TIMEOUT_SECONDS,
        )
        result = AskQuestionResult.model_validate(raw)
        text = f"{config.target} {result.question_text}".strip()
        api = TelegramApi.from_env(client=self.client)
        delivery = DeliveryService(
            self.root,
            api=api,
            chat_id=self._chat_id(),
            checkpoint=self.checkpoint,
            bot_id=self.bot_id,
            persist_mode=self.persist_mode,
        )
        try:
            await delivery.deliver(
                job_id=f"busy-question:{int(now.timestamp())}",
                text=text,
                reply_to_message_id=None,
                message_thread_id=None,
                now=now,
            )
        except (DeliveryAmbiguous, DeliveryFailed):
            return
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"busy-question:{int(now.timestamp())}"
        )
        uow.register_external_evidence(())
        service.stage_triggered(uow, now)
        uow.publish()

    async def _prepare_pending_replies(self) -> None:
        username = os.environ.get("TAWG_TELEGRAM_BOT_USERNAME")
        if self.member_introductions_enabled:
            MemberWelcomeReconciler(self.root).reconcile(now=self.now)
        if username:
            ReplyRepairReconciler(self.root, bot_username=username).reconcile(now=self.now)
        jobs = self._load_jobs()
        model_work_deferred = (
            self.knowledge_attempted or self.daily_attempted or self.prepared_daily is not None
        )
        actionable = [
            job
            for job in jobs
            if (
                job.trigger_kind is not TriggerKind.MEMBER_INTRODUCTION
                or (
                    self.member_introductions_enabled
                    and job.prerequisite_job_id is not None
                    and any(
                        prerequisite.job_id == job.prerequisite_job_id
                        and prerequisite.status is JobStatus.DELIVERED
                        for prerequisite in jobs
                    )
                )
            )
            if job.status is JobStatus.READY
            or (
                (
                    not model_work_deferred
                    or job.trigger_kind
                    in {TriggerKind.MEMBER_WELCOME, TriggerKind.MEMBER_INTRODUCTION}
                )
                and (
                    job.status is JobStatus.PENDING
                    or (
                        job.status is JobStatus.PROCESSING
                        and job.updated_at <= self.now - _PROCESSING_LEASE
                    )
                )
            )
        ]
        actionable.sort(
            key=lambda job: (
                job.status is not JobStatus.READY,
                job.safe_error_code is not None,
                job.updated_at,
                job.job_id,
            )
        )
        actionable = _filter_bot_local_jobs(
            actionable,
            bot_id=self.bot_id,
            persist_mode=self.persist_mode,
        )
        if not actionable:
            self.prepared_replies = []
            return
        if not username:
            raise RuntimeFailure("TAWG_TELEGRAM_BOT_USERNAME is not configured")
        reply_deadline = monotonic() + _REPLY_PHASE_BUDGET_SECONDS
        self.prepared_replies = []
        self.reply_failures = []
        for job in actionable[:_MAX_REPLIES_PER_TICK]:
            remaining_seconds = reply_deadline - monotonic()
            if remaining_seconds <= 0:
                break
            service = BotReplyService(
                self.root,
                ai=self.ai,
                bot_username=username,
                live_evidence=self.live_evidence,
                knowledge_state=self.knowledge_state,
                chat_id=(self._chat_id() if os.environ.get("TAWG_TELEGRAM_CHAT_ID") else None),
                timeout_seconds=min(_REPLY_TIMEOUT_SECONDS, remaining_seconds),
                scan_target_verifier=self.scan_target_verifier,
                github_current_client=getattr(self.github_announcements, "client", None),
                invinoveritas_client=self.invinoveritas_client,
                invinoveritas_api_key=self.invinoveritas_api_key,
            )
            try:
                prepared = await service.prepare(job.job_id, now=self.now)
                if prepared is not None:
                    self.prepared_replies.append(prepared)
            except ReplyRejected as error:
                self.reply_failures.append(f"{job.job_id}:{error.safe_code}")
                _safe_log("reply_prepare", error.safe_code)
                continue

    def _reload_registry(self) -> None:
        self.registry = SourceRegistry.from_yaml(self.root / "knowledge/meta/sources.yml")
        self.live_evidence.registry = self.registry
        self.knowledge_state = KnowledgeStateStore(self.root, registry=self.registry)

    def _load_jobs(self) -> list[PendingBotJob]:
        path = self.root / "data/state/pending-bot-jobs.json"
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeFailure("invalid pending bot job state")
        return [PendingBotJob.model_validate(item) for item in raw]

    @staticmethod
    def _chat_id() -> int:
        raw = os.environ.get("TAWG_TELEGRAM_CHAT_ID")
        if not raw:
            raise RuntimeFailure("TAWG_TELEGRAM_CHAT_ID is not configured")
        try:
            return int(raw)
        except ValueError:
            raise RuntimeFailure("TAWG_TELEGRAM_CHAT_ID must be an integer") from None

    @staticmethod
    def _github_announcement_topic_id() -> int:
        name = "TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID"
        raw = os.environ.get(name)
        if not raw:
            raise RuntimeFailure(f"{name} is not configured")
        try:
            topic_id = int(raw)
        except ValueError:
            raise RuntimeFailure(f"{name} must be a positive integer") from None
        if topic_id <= 0:
            raise RuntimeFailure(f"{name} must be a positive integer")
        return topic_id


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")


def _configured_bot_username() -> str:
    username = os.environ.get("TAWG_TELEGRAM_BOT_USERNAME")
    if not username:
        raise RuntimeFailure("TAWG_TELEGRAM_BOT_USERNAME is not configured")
    return username


def _filter_bot_local_jobs(
    jobs: list[PendingBotJob],
    *,
    bot_id: int | None,
    persist_mode: PersistMode,
) -> list[PendingBotJob]:
    if persist_mode is PersistMode.RECEIPT_ONLY and bot_id is not None:
        prefix = f"reply:{bot_id}:"
        return [job for job in jobs if job.job_id.startswith(prefix)]
    return jobs


def _authorized_rich_daily_preview_job_id(root: Path, window: DailyWindow) -> str:
    if window.window_id != _RICH_DAILY_PREVIEW_SOURCE_ID:
        return window.window_id
    attempts = _load_delivery_attempts(root)
    original = next(
        (attempt for attempt in attempts if attempt.delivery_id == _RICH_DAILY_PREVIEW_SOURCE_ID),
        None,
    )
    if (
        original is None
        or original.job_id != _RICH_DAILY_PREVIEW_SOURCE_ID
        or original.status is not DeliveryStatus.AMBIGUOUS
        or original.content_sha256 != _RICH_DAILY_PREVIEW_SOURCE_SHA256
        or original.telegram_message_ids
        or original.sent_at is not None
        or original.safe_error_code != "operator_superseded_no_delivery_evidence"
    ):
        return window.window_id
    return _RICH_DAILY_PREVIEW_JOB_ID


def _recover_rich_daily_preview(root: Path, *, delivery_job_id: str) -> PreparedDaily | None:
    attempts = _load_delivery_attempts(root)
    existing = next(
        (attempt for attempt in attempts if attempt.delivery_id == delivery_job_id),
        None,
    )
    if existing is not None and existing.status is DeliveryStatus.DELIVERED:
        return None
    if existing is not None and existing.status in {
        DeliveryStatus.SENDING,
        DeliveryStatus.AMBIGUOUS,
    }:
        raise RuntimeFailure("Rich Daily preview delivery requires operator review")
    path = root / "data/state/prepared-daily.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("invalid prepared Rich Daily preview state") from error
    if not isinstance(raw, dict) or raw.get("window_id") != delivery_job_id:
        return None
    text = raw.get("telegram_text")
    citations = raw.get("citations")
    quiet_day = raw.get("quiet_day", False)
    if (
        raw.get("schema") != "tawg.prepared-daily.v1"
        or not isinstance(text, str)
        or not text.strip()
        or not isinstance(citations, list)
        or any(not isinstance(item, str) for item in citations)
        or not isinstance(quiet_day, bool)
    ):
        raise RuntimeFailure("invalid prepared Rich Daily preview state")
    if (
        existing is not None
        and existing.content_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        raise RuntimeFailure("prepared Rich Daily preview failed delivery binding")
    return PreparedDaily(
        window_id=delivery_job_id,
        telegram_text=text,
        messages=(text,),
        citations=tuple(citations),
        quiet_day=quiet_day,
    )


def _load_delivery_attempts(root: Path) -> tuple[DeliveryAttempt, ...]:
    path = root / "data/state/delivery-state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError
        attempts = tuple(DeliveryAttempt.model_validate(item) for item in raw)
    except (
        OSError,
        UnicodeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeFailure("invalid delivery state for Rich Daily preview") from error
    if len({attempt.delivery_id for attempt in attempts}) != len(attempts):
        raise RuntimeFailure("duplicate delivery state for Rich Daily preview")
    return attempts


def _safe_log(phase: str, code: str, *, erc: int | None = None) -> None:
    suffix = f" erc={erc}" if erc is not None else ""
    print(f"tawg_event=phase_failed phase={phase} code={code}{suffix}", flush=True)
