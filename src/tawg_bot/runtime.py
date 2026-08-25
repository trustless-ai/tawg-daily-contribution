"""Production composition for the scheduled repository-backed bot."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from tawg_bot.bot_router import BotReplyService, PreparedReply, ReplyRejected
from tawg_bot.claude_cli import ClaudeCli
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
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_refresh import KnowledgeRefresh, RefreshResult
from tawg_bot.live_evidence import EvidencePack, LiveEvidenceService
from tawg_bot.models import JobStatus, PendingBotJob
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.scheduler import Scheduler
from tawg_bot.source_registry import SourceRegistry
from tawg_bot.telegram_api import TelegramApi
from tawg_bot.telegram_intake import TelegramIntake
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class RuntimeFailure(RuntimeError):
    """A safe production-composition failure."""


_SOURCE_RECHECK_INTERVAL = timedelta(hours=24)
_MAX_SCHEDULED_SOURCE_ERCS = 2
_SOURCE_ERC_TIMEOUT_SECONDS = 60
_KNOWLEDGE_TIMEOUT_SECONDS = 180
_DAILY_EVIDENCE_TIMEOUT_SECONDS = 60
_DAILY_TIMEOUT_SECONDS = 360
_REPLY_TIMEOUT_SECONDS = 120
_DAILY_REJECTION_CODES = {
    "Daily factual bullet lacks a valid citation": "daily_citation_invalid",
    "Daily synthesis contains source-dependent detail": "daily_synthesis_invalid",
    "Daily What moved has an invalid direction structure": "daily_structure_invalid",
    "Daily concrete progress uses an invalid bullet marker": "daily_structure_invalid",
    "active Daily lacks a complete What moved direction": "daily_structure_invalid",
    "Daily output has an unexpected top-level section": "daily_sections_invalid",
    "Daily text contains an unknown citation": "daily_citation_unknown",
    "Daily citation references unknown evidence": "daily_citation_unknown",
    "Daily output contains ranking or persona language": "daily_tone_invalid",
    "Daily output exceeds the emoji limit": "daily_tone_invalid",
    "Daily must integrate Appreciation into What moved": "daily_tone_invalid",
}


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
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(self.root, client=client, checkpoint=self.checkpoint, now=now)
            result = await Scheduler(self.root, pipeline=pipeline).tick(
                now, observe_only=observe_only
            )
        try:
            await self.checkpoint.publish(
                f"layer-success:{result.layer.name.casefold()}:{int(now.timestamp())}",
                self.root,
            )
        except Exception:
            _safe_log("final_checkpoint", "final_checkpoint_failed")

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
        )
        self.knowledge_state = KnowledgeStateStore(self.root, registry=self.registry)
        self.telegram_synced_at: datetime | None = None
        self.source_checked_at: datetime | None = None
        self.live_evidence_collected_at: datetime | None = None
        self.knowledge_refreshed_at: datetime | None = None
        self.prepared_daily: PreparedDaily | None = None
        self.prepared_replies: list[PreparedReply] = []

    async def telegram_intake(self, now: datetime) -> None:
        api = TelegramApi.from_env(client=self.client)
        intake = TelegramIntake.from_env(root=self.root, api=api)
        await intake.collect(now)
        self.telegram_synced_at = now
        await self.checkpoint.publish(f"telegram-intake:{int(now.timestamp())}", self.root)

    async def source_check(self, now: datetime) -> None:
        erc_numbers = self.registry.due_erc_numbers(
            now,
            max_age=_SOURCE_RECHECK_INTERVAL,
        )[:_MAX_SCHEDULED_SOURCE_ERCS]
        incomplete = False
        for erc_number in erc_numbers:
            try:
                await asyncio.wait_for(
                    self.check_sources(
                        now, erc_numbers=(erc_number,), observe_only=False
                    ),
                    timeout=_SOURCE_ERC_TIMEOUT_SECONDS,
                )
                await self.checkpoint.publish(
                    f"source-check:erc-{erc_number}:{int(now.timestamp())}",
                    self.root,
                )
            except Exception:
                incomplete = True
                _safe_log("source_check", "source_check_failed", erc=erc_number)
        self.source_checked_at = now
        if incomplete:
            raise RuntimeFailure("source check incomplete")

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
        erc_numbers = self.knowledge_state.eligible_refresh_erc_numbers(cutoff)
        if not erc_numbers:
            self.knowledge_refreshed_at = self.now
            return
        erc_number = erc_numbers[0]
        operation_id = f"knowledge-refresh-{cutoff.strftime('%Y%m%dt%H%M%Sz')}"
        service = KnowledgeRefresh(
            self.root,
            ai=self.ai,
            live_evidence=self.live_evidence,
            registry=self.registry,
            max_ercs_per_run=1,
            timeout_seconds=_KNOWLEDGE_TIMEOUT_SECONDS,
        )
        try:
            await service.run(
                cutoff=cutoff,
                operation_id=operation_id,
                erc_numbers=frozenset({erc_number}),
                dry_run=False,
            )
        except Exception:
            try:
                uow = RepositoryUnitOfWork(
                    self.root,
                    operation_id=(
                        f"knowledge-deferred:erc-{erc_number}:{int(cutoff.timestamp())}"
                    ),
                )
                uow.register_external_evidence(())
                self.knowledge_state.defer_refresh_erc(
                    uow,
                    erc_number,
                    now=cutoff,
                    safe_error_code="knowledge_refresh_failed",
                )
                uow.publish()
                await self.checkpoint.publish(
                    f"knowledge-deferred:erc-{erc_number}:{int(cutoff.timestamp())}",
                    self.root,
                )
            except Exception:
                _safe_log("knowledge_refresh", "knowledge_deferral_failed", erc=erc_number)
                raise RuntimeFailure("knowledge deferral incomplete") from None
            _safe_log("knowledge_refresh", "knowledge_refresh_failed", erc=erc_number)
            raise RuntimeFailure("knowledge refresh deferred") from None
        try:
            await self.checkpoint.publish(
                f"knowledge-refresh:erc-{erc_number}:{int(cutoff.timestamp())}",
                self.root,
            )
        except Exception:
            _safe_log("knowledge_refresh", "knowledge_checkpoint_failed", erc=erc_number)
            raise RuntimeFailure("knowledge checkpoint incomplete") from None
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
        try:
            prepared = await self.prepare_daily(window, dry_run=False)
        except PersistenceRejected:
            self.prepared_daily = None
            _safe_log("daily_persistence", "daily_persistence_rejected")
            raise RuntimeFailure("Daily persistence failed") from None
        except DailyRejected as error:
            self.prepared_daily = None
            code = _DAILY_REJECTION_CODES.get(str(error), "daily_validation_failed")
            _safe_log("daily_validation", code)
            raise RuntimeFailure("Daily validation failed") from None
        except Exception:
            self.prepared_daily = None
            raise
        if prepared is None:
            return
        try:
            await self.checkpoint.publish(
                f"daily-prepared:{int(window.end.timestamp())}", self.root
            )
        except Exception:
            self.prepared_daily = None
            _safe_log("daily_prepare", "daily_checkpoint_failed")
            raise RuntimeFailure("Daily checkpoint incomplete") from None

    async def prepare_daily(self, window: DailyWindow, *, dry_run: bool) -> PreparedDaily | None:
        ready_at = self.now
        evidence = await DailyEvidenceCollector(
            self.root,
            github=GitHubActivityRecords(self.root, client=self.client),
            magicians=MagiciansActivityRecords(
                self.root, client=self.client, registry=self.registry
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
        ).prepare(
            window, readiness=readiness, evidence=evidence
        )
        if prepared is None:
            self.prepared_daily = None
            return None
        if dry_run:
            self.prepared_daily = prepared
            return prepared
        artifact = {
            "schema": "tawg.prepared-daily.v1",
            "dry_run": dry_run,
            "window_id": prepared.window_id,
            "telegram_text": prepared.telegram_text,
            "citations": list(prepared.citations),
            "prepared_at": self.now.isoformat().replace("+00:00", "Z"),
        }
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"daily-prepared:{int(window.end.timestamp())}"
        )
        uow.register_external_evidence(
            item.text for item in evidence if item.source_kind != "telegram"
        )
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
        )
        for reply in self.prepared_replies:
            try:
                await delivery.deliver(
                    job_id=reply.job_id,
                    text=reply.reply_text,
                    reply_to_message_id=reply.reply_to_message_id,
                    message_thread_id=reply.message_thread_id,
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

    async def _prepare_pending_replies(self) -> None:
        jobs = self._load_jobs()
        actionable_statuses = (
            {JobStatus.READY}
            if self.prepared_daily is not None
            else {JobStatus.PENDING, JobStatus.READY}
        )
        actionable = [job for job in jobs if job.status in actionable_statuses]
        actionable.sort(
            key=lambda job: (
                job.status is not JobStatus.READY,
                job.updated_at,
                job.job_id,
            )
        )
        if not actionable:
            self.prepared_replies = []
            return
        username = os.environ.get("TAWG_TELEGRAM_BOT_USERNAME")
        if not username:
            raise RuntimeFailure("TAWG_TELEGRAM_BOT_USERNAME is not configured")
        service = BotReplyService(
            self.root,
            ai=self.ai,
            bot_username=username,
            live_evidence=self.live_evidence,
            knowledge_state=self.knowledge_state,
            timeout_seconds=_REPLY_TIMEOUT_SECONDS,
        )
        self.prepared_replies = []
        for job in actionable[:1]:
            try:
                self.prepared_replies.append(await service.prepare(job.job_id, now=self.now))
            except ReplyRejected:
                _safe_log("reply_prepare", "reply_prepare_failed")
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


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")


def _safe_log(phase: str, code: str, *, erc: int | None = None) -> None:
    suffix = f" erc={erc}" if erc is not None else ""
    print(f"tawg_event=phase_failed phase={phase} code={code}{suffix}", flush=True)
