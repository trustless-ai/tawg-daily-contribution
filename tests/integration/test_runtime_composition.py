from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.bot_router import PreparedReply, ReplyRejected
from tawg_bot.claude_cli import ClaudeCli as RealClaudeCli
from tawg_bot.claude_cli import ClaudeCliError, CompletedProcess
from tawg_bot.daily import DailyRejected, DailyWindow, PreparedDaily
from tawg_bot.daily_evidence import DailyEvidence
from tawg_bot.github_announcements import (
    GitHubAnnouncementBatch,
    GitHubAnnouncementState,
)
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_refresh import RefreshResult
from tawg_bot.live_evidence import LiveEvidenceService
from tawg_bot.models import DeliveryStatus, JobStatus, PendingBotJob, TriggerKind
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.repository_session import CommandResult, RepositoryConflict, RepositorySession
from tawg_bot.runtime import (
    ProductionRuntime,
    RuntimeFailure,
    ScriptCheckpoint,
    SourceCheckSummary,
    _LivePipeline,
)
from tawg_bot.scheduler import IntakePolicy
from tawg_bot.scoped_scanner import ScopedScanResult
from tawg_bot.source_registry import EvidenceKind, SourceRegistry
from tawg_bot.telegram_api import SentMessage, TelegramApiError
from tawg_bot.telegram_intake import ingest_envelopes
from tawg_bot.telegram_webhook import TelegramWebhookEntity, TelegramWebhookEnvelope
from tests.integration.test_live_knowledge_refresh import _pack
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


class EmptyAnnouncements:
    async def scan(self, *, now: datetime) -> GitHubAnnouncementBatch:
        return GitHubAnnouncementBatch(
            state=GitHubAnnouncementState(initialized_at=now, repositories=()),
            pending=(),
        )

    def stage(self, batch: GitHubAnnouncementBatch, uow: Any) -> None:
        uow.stage_json(
            "data/state/github-announcement-state.json",
            batch.state.model_dump(mode="json"),
        )
        uow.stage_json("data/state/pending-github-announcements.json", [])


class LiveEvidence:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def build(self, query, *, now):
        assert now == NOW
        self.calls.append(query)
        return _pack().model_copy(update={"query": query})


@pytest.mark.asyncio
async def test_member_introduction_waits_for_the_l1_enabled_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)
    direct = PendingBotJob(
        job_id="member-welcome:logan",
        trigger_record_id="tg:tawg:101",
        reply_to_message_id=None,
        message_thread_id=88,
        trigger_kind=TriggerKind.MEMBER_WELCOME,
        status=JobStatus.DELIVERED,
        prepared_reply_text="@LoganVerdict Welcome!",
        prepared_language="en",
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:100",
        created_at=NOW,
        updated_at=NOW,
    )
    introduction = PendingBotJob(
        job_id="member-introduction:logan",
        trigger_record_id="tg:tawg:101",
        reply_to_message_id=None,
        message_thread_id=88,
        trigger_kind=TriggerKind.MEMBER_INTRODUCTION,
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:100",
        prerequisite_job_id=direct.job_id,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps(
            [direct.model_dump(mode="json"), introduction.model_dump(mode="json")]
        )
        + "\n",
        encoding="utf-8",
    )
    prepared_ids: list[str] = []

    class NoopReconciler:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def reconcile(self, *, now: datetime) -> int:
            assert now == NOW
            return 0

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            prepared_ids.append(job_id)
            return PreparedReply(
                job_id=job_id,
                reply_to_message_id=None,
                message_thread_id=88,
                reply_text="@LoganVerdict Welcome to Trustless AI.",
                citations=(),
                language="en",
                refusal=False,
            )

    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "bot")
    monkeypatch.setattr(runtime_module, "MemberWelcomeReconciler", NoopReconciler)
    monkeypatch.setattr(runtime_module, "ReplyRepairReconciler", NoopReconciler)
    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    async with httpx.AsyncClient() as client:
        webhook_pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=Checkpoint(),
            now=NOW,
        )
        await webhook_pipeline._prepare_pending_replies()
        assert prepared_ids == []

        l1_pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=Checkpoint(),
            now=NOW,
            member_introductions_enabled=True,
        )
        await l1_pipeline._prepare_pending_replies()

    assert prepared_ids == [introduction.job_id]


def scaffold(root: Path) -> None:
    copy_static_runtime_tree(ROOT, root)


def webhook_envelope(
    *, update_id: int, message_id: int, message_thread_id: int
) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=False,
        text="@tawg_bot status?",
        public_username="alice_tawg",
        display_name="Alice",
        message_thread_id=message_thread_id,
        entities=(
            TelegramWebhookEntity(entity_type="mention", offset=0, length=9, value="@tawg_bot"),
        ),
        triggers_reply=True,
        integrity_digest="0" * 64,
    )
    payload = value.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value.model_copy(update={"integrity_digest": digest})


@pytest.mark.asyncio
async def test_live_pipeline_checks_sources_without_external_body_mirrors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        assert isinstance(pipeline.live_evidence, LiveEvidenceService)
        assert pipeline.live_evidence.operation_seconds == 45
        assert isinstance(pipeline.knowledge_state, KnowledgeStateStore)
        assert {8004, 8183}.issubset(pipeline.registry.erc_numbers())
        pipeline.live_evidence = LiveEvidence()

        summary = await pipeline.check_sources(NOW, erc_numbers=(8004,), observe_only=False)

        assert summary.erc_count == 1
        assert summary.evidence_count == 2
        assert summary.gap_count == 1
        assert summary.persisted
        assert not (tmp_path / "data/github").exists()
        assert not (tmp_path / "data/magicians").exists()
        registry_text = (tmp_path / "knowledge/meta/sources.yml").read_text()
        assert _pack().evidence[0].content_sha256 in registry_text

        class Refresh:
            def __init__(
                self,
                root: Path,
                *,
                ai: Any,
                live_evidence: Any,
                registry: Any,
                **kwargs: Any,
            ) -> None:
                assert kwargs == {"max_ercs_per_run": 1, "timeout_seconds": 180}
                del root, ai, live_evidence, registry

            async def run(self, **kwargs: Any) -> RefreshResult:
                assert kwargs["cutoff"] == NOW
                return RefreshResult((), (), False)

        monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
        await pipeline.knowledge_refresh(NOW)
        assert pipeline.knowledge_refreshed_at == NOW
        await pipeline.validate()

        prepared = PreparedDaily(
            window_id="daily:2026-08-23T23:00:00Z",
            telegram_text="Hello",
            messages=("Hello",),
            citations=(),
            quiet_day=True,
        )

        class Daily:
            def __init__(self, root: Path, *, ai: Any, timeout_seconds: float) -> None:
                assert timeout_seconds == 900
                del root, ai

            async def prepare(
                self,
                window: DailyWindow,
                *,
                readiness: Any,
                evidence: Any,
            ) -> PreparedDaily:
                assert window.window_id == prepared.window_id
                assert readiness.live_evidence_collected_at == NOW
                assert evidence == ()
                return prepared

        class Collector:
            def __init__(self, root: Path, **kwargs: Any) -> None:
                assert kwargs["timeout_seconds"] == 60
                del root

            async def collect(self, window: DailyWindow, *, now: datetime) -> tuple:
                assert window.window_id == prepared.window_id
                assert now == NOW
                return ()

        monkeypatch.setattr(runtime_module, "DailyService", Daily)
        monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
        pipeline.telegram_synced_at = NOW
        pipeline.source_checked_at = NOW
        prepared_daily_path = tmp_path / "data/state/prepared-daily.json"
        prepared_daily_before = (
            prepared_daily_path.read_bytes() if prepared_daily_path.exists() else None
        )
        await pipeline.prepare_daily(DailyWindow.for_due_run(NOW), dry_run=True)
        assert pipeline.prepared_daily == prepared
        prepared_daily_after = (
            prepared_daily_path.read_bytes() if prepared_daily_path.exists() else None
        )
        assert prepared_daily_after == prepared_daily_before

        async def no_replies() -> None:
            pipeline.prepared_replies = []

        monkeypatch.setattr(pipeline, "_prepare_pending_replies", no_replies)
        await pipeline.publish_repository()
        assert checkpoint.operations[-1].startswith("prepared:")


@pytest.mark.asyncio
async def test_source_check_observe_only_does_not_change_repository(
    tmp_path: Path,
) -> None:
    scaffold(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for base in (tmp_path / "knowledge", tmp_path / "data/state")
        for path in base.rglob("*")
        if path.is_file()
    }
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.live_evidence = LiveEvidence()
        summary = await pipeline.check_sources(NOW, erc_numbers=(8004,), observe_only=True)
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for base in (tmp_path / "knowledge", tmp_path / "data/state")
        for path in base.rglob("*")
        if path.is_file()
    }
    assert not summary.persisted
    assert after == before


@pytest.mark.asyncio
async def test_consecutive_source_batches_retain_both_registry_observations(
    tmp_path: Path,
) -> None:
    scaffold(tmp_path)
    hashes = {8004: "8" * 64, 8183: "1" * 64}
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)

        class Evidence:
            async def build(self, query: Any, *, now: datetime) -> Any:
                erc_number = query.erc_numbers[0]
                source = pipeline.registry.resolve(
                    erc_number, frozenset(EvidenceKind)
                )[0]
                item = _pack().evidence[0].model_copy(
                    update={
                        "erc_number": erc_number,
                        "source_key": source.source_key,
                        "kind": source.kind,
                        "authority": source.authority,
                        "canonical_url": source.canonical_url,
                        "citation_url": source.canonical_url,
                        "observed_at": now,
                        "content_sha256": hashes[erc_number],
                    }
                )
                return _pack().model_copy(
                    update={
                        "query": query,
                        "evidence": [item],
                        "citation_allowlist": [item.citation_url],
                        "missing_required": [],
                    }
                )

        pipeline.live_evidence = Evidence()  # type: ignore[assignment]
        await pipeline.check_sources(NOW, erc_numbers=(8004,), observe_only=False)
        await pipeline.check_sources(NOW, erc_numbers=(8183,), observe_only=False)

    registry = SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml")
    for erc_number, expected_hash in hashes.items():
        source = registry.resolve(erc_number, frozenset(EvidenceKind))[0]
        assert source.last_observed is not None
        assert source.last_observed.content_sha256 == expected_hash


@pytest.mark.asyncio
async def test_scheduled_source_check_uses_scoped_scanner(tmp_path: Path) -> None:
    scaffold(tmp_path)
    calls: list[tuple[datetime, datetime]] = []

    class Scanner:
        async def scan(self, *, since: datetime, now: datetime) -> ScopedScanResult:
            calls.append((since, now))
            return ScopedScanResult(
                observations=(),
                github_cursors={},
                magicians_cursors={},
                failed_sources=(),
            )

        def stage(self, result: ScopedScanResult, uow: Any) -> None:
            del result
            uow.stage_json("data/state/scoped-source-observations.json", [])

        def stage_repository_pages(
            self,
            uow: Any,
            repositories: object,
            now: datetime,
        ) -> None:
            del uow, repositories, now

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.scoped_scanner = Scanner()  # type: ignore[assignment]
        pipeline.github_announcements = EmptyAnnouncements()  # type: ignore[assignment]

        await pipeline.source_check(NOW)

    assert calls == [(NOW - timedelta(hours=24), NOW)]
    assert pipeline.source_checked_at == NOW


@pytest.mark.asyncio
async def test_telegram_intake_checkpoints_before_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()

    class Api:
        @classmethod
        def from_env(cls, *, client: Any) -> object:
            del client
            return object()

    class Intake:
        @classmethod
        def from_env(cls, *, root: Path, api: Any) -> Intake:
            assert root == tmp_path
            del api
            return cls()

        async def collect(self, now: datetime) -> None:
            assert now == NOW

    monkeypatch.setattr(runtime_module, "TelegramApi", Api)
    monkeypatch.setattr(runtime_module, "TelegramIntake", Intake)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        await pipeline.telegram_intake(NOW)

    assert checkpoint.operations == [f"telegram-intake:{int(NOW.timestamp())}"]


@pytest.mark.asyncio
async def test_explicit_polling_intake_calls_get_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    cursor_path = tmp_path / "data/state/source-cursors.json"
    cursors = json.loads(cursor_path.read_text())
    cursors["telegram_offset"] = 73
    cursor_path.write_text(json.dumps(cursors) + "\n", encoding="utf-8")
    calls: list[tuple[int, int]] = []

    class Api:
        @classmethod
        def from_env(cls, *, client: Any) -> Api:
            del client
            return cls()

        async def get_all_updates(self, offset: int, limit: int = 100) -> list[Any]:
            calls.append((offset, limit))
            return []

    monkeypatch.setattr(runtime_module, "TelegramApi", Api)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-100123")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path, client=client, checkpoint=Checkpoint(), now=NOW
        )
        await pipeline.telegram_intake(NOW)

    assert calls == [(73, 100)]


@pytest.mark.asyncio
async def test_scheduled_source_scan_is_bounded_and_logs_safe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    timeouts: list[float] = []
    original_wait_for = asyncio.wait_for

    async def bounded(awaitable: Any, *, timeout: float) -> Any:
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(runtime_module.asyncio, "wait_for", bounded)

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)

        class Scanner:
            async def scan(self, **kwargs: Any) -> ScopedScanResult:
                del kwargs
                raise RuntimeError("provider-secret-body")

        pipeline.scoped_scanner = Scanner()  # type: ignore[assignment]
        with pytest.raises(RuntimeFailure, match="source check incomplete"):
            await pipeline.source_check(NOW)

    assert timeouts == [60]
    assert checkpoint.operations == []
    captured = capsys.readouterr().out
    assert "code=source_check_failed" in captured
    assert "provider-secret-body" not in captured


@pytest.mark.asyncio
async def test_scheduled_knowledge_refresh_is_a_model_free_compatibility_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    class Refresh:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("scheduled compatibility phase must not construct a model job")

    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=Checkpoint(),
            now=NOW,
        )
        await pipeline.knowledge_refresh(NOW)

    assert pipeline.knowledge_refreshed_at == NOW


@pytest.mark.asyncio
async def test_scheduled_daily_checkpoints_before_reply_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    window = DailyWindow.for_due_run(NOW)
    prepared = PreparedDaily(window.window_id, "Daily", ("Daily",), (), True)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)

        async def prepare_daily(
            selected: DailyWindow, *, dry_run: bool
        ) -> PreparedDaily:
            assert selected == window
            assert not dry_run
            pipeline.prepared_daily = prepared
            return prepared

        monkeypatch.setattr(pipeline, "prepare_daily", prepare_daily)
        await pipeline.daily_prepare(window.window_id)

    assert checkpoint.operations == [f"daily-prepared:{int(window.end.timestamp())}"]


@pytest.mark.asyncio
async def test_prepare_daily_does_not_expose_output_before_artifact_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    prepared = PreparedDaily(window.window_id, "Daily", ("Daily",), (), True)

    class Daily:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def prepare(self, *args: Any, **kwargs: Any) -> PreparedDaily:
            del args, kwargs
            return prepared

    class Collector:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def collect(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
            del args, kwargs
            return ()

    class RejectingUnitOfWork:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        def register_external_evidence(self, evidence: Any) -> None:
            del evidence

        def stage_json(self, path: str, artifact: Any) -> None:
            del path, artifact

        def publish(self) -> None:
            raise PersistenceRejected

    monkeypatch.setattr(runtime_module, "DailyService", Daily)
    monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
    monkeypatch.setattr(runtime_module, "RepositoryUnitOfWork", RejectingUnitOfWork)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        with pytest.raises(PersistenceRejected):
            await pipeline.prepare_daily(window, dry_run=False)

    assert pipeline.prepared_daily is None


@pytest.mark.asyncio
async def test_prepare_daily_persists_an_exact_validated_external_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    locator = (
        "https://github.com/trustless-ai/recompute-kit/pull/15#issuecomment-5425745065"
    )
    evidence = DailyEvidence(
        evidence_id="gh:recompute-kit:comment:5425745065",
        source_kind="github",
        source_url=locator,
        created_at=window.start + timedelta(hours=1),
        updated_at=window.start + timedelta(hours=1),
        author_person_id="zexoverz",
        text=(
            "Related review: "
            "https://github.com/trustless-ai/recompute-kit/issues/99"
        ),
    )
    prepared = PreparedDaily(window.window_id, "Daily", ("Daily",), (locator,), False)

    class Daily:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def prepare(self, *args: Any, **kwargs: Any) -> PreparedDaily:
            del args, kwargs
            return prepared

    class Collector:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def collect(self, *args: Any, **kwargs: Any) -> tuple[DailyEvidence, ...]:
            del args, kwargs
            return (evidence,)

    monkeypatch.setattr(runtime_module, "DailyService", Daily)
    monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        result = await pipeline.prepare_daily(window, dry_run=False)

    assert result == prepared
    artifact = json.loads(
        (tmp_path / "data/state/prepared-daily.json").read_text(encoding="utf-8")
    )
    assert artifact["citations"] == [locator]


@pytest.mark.asyncio
async def test_operator_preview_generates_fresh_rich_daily_under_a_distinct_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    preview_now = datetime(2026, 8, 28, 1, 17, tzinfo=UTC)
    window = DailyWindow.for_due_run(preview_now)
    assert window.window_id == "daily:2026-08-27T23:00:00Z"
    (tmp_path / "data/state/prepared-daily.json").unlink(missing_ok=True)
    state = tmp_path / "data/state/delivery-state.json"
    state.write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": window.window_id,
                    "job_id": window.window_id,
                    "destination": "tawg",
                    "status": "ambiguous",
                    "content_sha256": (
                        "78bb59915ac2873a30dedb7416e039ade"
                        "46b80ba602c4e20e85e5d842e507262"
                    ),
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "reply_to_message_id": None,
                    "message_thread_id": None,
                    "telegram_chat_id": None,
                    "telegram_message_ids": [],
                    "sent_at": None,
                    "prepared_at": "2026-08-27T23:02:53.169597Z",
                    "updated_at": "2026-08-28T01:56:56Z",
                    "safe_error_code": "operator_superseded_no_delivery_evidence",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generated = PreparedDaily(
        window.window_id,
        "NEW CURRENT RICH DAILY",
        ("NEW CURRENT RICH DAILY",),
        (),
        True,
    )
    calls: list[str] = []

    class Daily:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def prepare(self, selected: DailyWindow, **kwargs: Any) -> PreparedDaily:
            del kwargs
            calls.append(selected.window_id)
            return generated

    class Collector:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def collect(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
            del args, kwargs
            return ()

    monkeypatch.setattr(runtime_module, "DailyService", Daily)
    monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path, client=client, checkpoint=Checkpoint(), now=preview_now
        )
        result = await pipeline.prepare_daily(window, dry_run=False)

    preview_id = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
    assert calls == [window.window_id]
    assert result is not None
    assert result.window_id == preview_id
    assert result.telegram_text == "NEW CURRENT RICH DAILY"
    artifact = json.loads(
        (tmp_path / "data/state/prepared-daily.json").read_text(encoding="utf-8")
    )
    assert artifact["window_id"] == preview_id
    assert artifact["telegram_text"] == "NEW CURRENT RICH DAILY"
    old_attempt = json.loads(state.read_text(encoding="utf-8"))[0]
    assert old_attempt["delivery_id"] == window.window_id
    assert old_attempt["status"] == "ambiguous"


@pytest.mark.parametrize("status", ["prepared", "failed"])
def test_operator_preview_recovers_the_exact_persisted_artifact(
    tmp_path: Path, status: str
) -> None:
    scaffold(tmp_path)
    preview_id = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
    text = "RECOVERED CURRENT RICH DAILY"
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": preview_id,
                    "job_id": preview_id,
                    "status": status,
                    "content_sha256": content_sha,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "prepared_at": "2026-08-28T01:00:00Z",
                    "updated_at": "2026-08-28T01:01:00Z",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "data/state/prepared-daily.json").write_text(
        json.dumps(
            {
                "schema": "tawg.prepared-daily.v1",
                "window_id": preview_id,
                "telegram_text": text,
                "citations": ["tg:tawg:3454"],
                "quiet_day": False,
                "prepared_at": "2026-08-28T01:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = runtime_module._recover_rich_daily_preview(
        tmp_path, delivery_job_id=preview_id
    )

    assert recovered is not None
    assert recovered.window_id == preview_id
    assert recovered.telegram_text == text
    assert recovered.citations == ("tg:tawg:3454",)


def test_operator_preview_rejects_a_changed_persisted_artifact(tmp_path: Path) -> None:
    scaffold(tmp_path)
    preview_id = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": preview_id,
                    "job_id": preview_id,
                    "status": "prepared",
                    "content_sha256": "0" * 64,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "prepared_at": "2026-08-28T01:00:00Z",
                    "updated_at": "2026-08-28T01:01:00Z",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "data/state/prepared-daily.json").write_text(
        json.dumps(
            {
                "schema": "tawg.prepared-daily.v1",
                "window_id": preview_id,
                "telegram_text": "CHANGED",
                "citations": [],
                "quiet_day": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeFailure, match="failed delivery binding"):
        runtime_module._recover_rich_daily_preview(
            tmp_path, delivery_job_id=preview_id
        )


@pytest.mark.parametrize("status", ["sending", "ambiguous"])
def test_operator_preview_never_retries_an_unknown_delivery(
    tmp_path: Path, status: str
) -> None:
    scaffold(tmp_path)
    preview_id = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": preview_id,
                    "job_id": preview_id,
                    "status": status,
                    "content_sha256": "0" * 64,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "prepared_at": "2026-08-28T01:00:00Z",
                    "updated_at": "2026-08-28T01:01:00Z",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeFailure, match="requires operator review"):
        runtime_module._recover_rich_daily_preview(
            tmp_path, delivery_job_id=preview_id
        )


@pytest.mark.asyncio
async def test_operator_preview_delivered_state_is_a_model_free_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    preview_now = datetime(2026, 8, 28, 1, 17, tzinfo=UTC)
    window = DailyWindow.for_due_run(preview_now)
    preview_id = "daily:preview-rich-v1:2026-08-27T23:00:00Z"
    original_sha = (
        "78bb59915ac2873a30dedb7416e039ade"
        "46b80ba602c4e20e85e5d842e507262"
    )
    attempts = [
        {
            "schema_version": "tawg.delivery-attempt.v1",
            "delivery_id": window.window_id,
            "job_id": window.window_id,
            "status": "ambiguous",
            "content_sha256": original_sha,
            "message_count": 1,
            "delivery_format": "rich_markdown_v1",
            "prepared_at": "2026-08-27T23:02:53Z",
            "updated_at": "2026-08-28T01:56:56Z",
            "safe_error_code": "operator_superseded_no_delivery_evidence",
        },
        {
            "schema_version": "tawg.delivery-attempt.v1",
            "delivery_id": preview_id,
            "job_id": preview_id,
            "status": "delivered",
            "content_sha256": "1" * 64,
            "message_count": 1,
            "delivery_format": "rich_markdown_v1",
            "telegram_message_ids": [5000],
            "sent_at": "2026-08-28T02:00:00Z",
            "prepared_at": "2026-08-28T01:59:00Z",
            "updated_at": "2026-08-28T02:00:00Z",
        },
    ]
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(attempts) + "\n", encoding="utf-8"
    )

    class Daily:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("delivered preview must not call the model")

    monkeypatch.setattr(runtime_module, "DailyService", Daily)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path, client=client, checkpoint=Checkpoint(), now=preview_now
        )
        result = await pipeline.prepare_daily(window, dry_run=False)

    assert result is None
    assert pipeline.prepared_daily is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejection", "safe_code", "sensitive_fragment"),
    [
        (
            "Daily factual bullet lacks a valid citation",
            "daily_citation_invalid",
            "factual bullet",
        ),
        (
            "Daily contributor lacks a confirmed Telegram mention",
            "daily_mention_invalid",
            "Telegram mention",
        ),
        (
            "invalid Daily model output",
            "daily_model_output_invalid",
            "model output",
        ),
        (
            "Daily title must match the exact UTC window",
            "daily_title_invalid",
            "exact UTC window",
        ),
        (
            "Daily title must match the Rich Markdown contract",
            "daily_title_invalid",
            "Rich Markdown",
        ),
        (
            "Daily UTC window must match the fixed window",
            "daily_window_invalid",
            "fixed window",
        ),
        (
            "Daily highlight lacks a valid citation",
            "daily_citation_invalid",
            "highlight",
        ),
        (
            "Daily Trusty's take contains source-dependent detail",
            "daily_grounding_invalid",
            "source-dependent detail",
        ),
        (
            "Daily concrete progress bullet uses an invalid Rich Markdown list marker",
            "daily_structure_invalid",
            "list marker",
        ),
        (
            "Daily output has an invalid required section: Next up",
            "daily_sections_invalid",
            "Next up",
        ),
    ],
)
async def test_scheduled_daily_logs_bounded_validation_code_without_raw_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rejection: str,
    safe_code: str,
    sensitive_fragment: str,
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)

        async def prepare_daily(selected: DailyWindow, *, dry_run: bool) -> PreparedDaily:
            assert selected == window
            assert not dry_run
            raise DailyRejected(rejection)

        monkeypatch.setattr(pipeline, "prepare_daily", prepare_daily)
        with pytest.raises(RuntimeFailure, match="Daily validation failed"):
            await pipeline.daily_prepare(window.window_id)

    captured = capsys.readouterr().out
    assert f"code={safe_code}" in captured
    assert sensitive_fragment not in captured


@pytest.mark.asyncio
async def test_scheduled_daily_logs_model_timeout_without_provider_error_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)

        async def prepare_daily(selected: DailyWindow, *, dry_run: bool) -> PreparedDaily:
            assert selected == window
            assert not dry_run
            raise ClaudeCliError("Claude Code exceeded its time limit")

        monkeypatch.setattr(pipeline, "prepare_daily", prepare_daily)
        with pytest.raises(RuntimeFailure, match="Daily model failed"):
            await pipeline.daily_prepare(window.window_id)

    captured = capsys.readouterr().out
    assert "code=daily_model_timeout" in captured
    assert "Claude Code" not in captured


@pytest.mark.asyncio
async def test_scheduled_daily_clears_prepared_output_after_persistence_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    prepared = PreparedDaily(window.window_id, "Daily", ("Daily",), (), True)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)

        async def reject_persistence(
            selected: DailyWindow, *, dry_run: bool
        ) -> PreparedDaily:
            assert selected == window
            assert not dry_run
            pipeline.prepared_daily = prepared
            raise PersistenceRejected

        monkeypatch.setattr(pipeline, "prepare_daily", reject_persistence)
        with pytest.raises(RuntimeFailure, match="persistence"):
            await pipeline.daily_prepare(window.window_id)

    assert pipeline.prepared_daily is None
    captured = capsys.readouterr().out
    assert "code=daily_persistence_rejected" in captured
    assert "persistence policy rejection" not in captured


@pytest.mark.asyncio
async def test_reply_preparation_processes_at_most_ten_pending_jobs_per_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id=f"reply:tg:tawg:{message_id}",
            trigger_record_id=f"tg:tawg:{message_id}",
            reply_to_message_id=message_id,
            created_at=NOW,
            updated_at=NOW,
        )
        for message_id in range(10, 22)
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    prepared_ids: list[str] = []

    class ReplyService:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            assert root == tmp_path
            assert kwargs["timeout_seconds"] == 300

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            prepared_ids.append(job_id)
            message_id = int(job_id.rsplit(":", 1)[-1])
            return PreparedReply(job_id, message_id, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert prepared_ids == [f"reply:tg:tawg:{message_id}" for message_id in range(10, 20)]


@pytest.mark.asyncio
async def test_reply_preparation_reclaims_only_expired_processing_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id="reply:tg:tawg:10",
            trigger_record_id="tg:tawg:10",
            reply_to_message_id=10,
            status=JobStatus.PROCESSING,
            created_at=NOW - timedelta(minutes=20),
            updated_at=NOW - timedelta(minutes=11),
        ),
        PendingBotJob(
            job_id="reply:tg:tawg:11",
            trigger_record_id="tg:tawg:11",
            reply_to_message_id=11,
            status=JobStatus.PROCESSING,
            created_at=NOW - timedelta(minutes=2),
            updated_at=NOW - timedelta(minutes=1),
        ),
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    prepared_ids: list[str] = []

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            prepared_ids.append(job_id)
            return PreparedReply(job_id, 10, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert prepared_ids == ["reply:tg:tawg:10"]


@pytest.mark.asyncio
async def test_reply_preparation_does_not_deliver_an_ai_ignored_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    job = PendingBotJob(
        job_id="reply:tg:tawg:10",
        trigger_record_id="tg:tawg:10",
        reply_to_message_id=10,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(
            self, job_id: str, *, now: datetime
        ) -> PreparedReply | None:
            assert job_id == "reply:tg:tawg:10"
            assert now == NOW
            return None

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert pipeline.prepared_replies == []


@pytest.mark.asyncio
async def test_reply_preparation_reconciles_policy_repairs_before_selecting_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    (tmp_path / "data/state/pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")
    reconciled: list[datetime] = []

    class Reconciler:
        def __init__(self, root: Path, *, bot_username: str) -> None:
            assert root == tmp_path
            assert bot_username == "tawg_bot"

        def reconcile(self, *, now: datetime) -> tuple[str, ...]:
            reconciled.append(now)
            return ()

    monkeypatch.setattr(runtime_module, "ReplyRepairReconciler", Reconciler)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert reconciled == [NOW]
    assert pipeline.prepared_replies == []


@pytest.mark.asyncio
async def test_reply_preparation_processes_fresh_work_before_a_failed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id="reply:tg:tawg:10",
            trigger_record_id="tg:tawg:10",
            reply_to_message_id=10,
            created_at=NOW - timedelta(minutes=2),
            updated_at=NOW - timedelta(minutes=1),
            safe_error_code="reply_prepare_failed",
        ),
        PendingBotJob(
            job_id="reply:tg:tawg:11",
            trigger_record_id="tg:tawg:11",
            reply_to_message_id=11,
            created_at=NOW,
            updated_at=NOW,
        ),
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    prepared_ids: list[str] = []

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            del now
            prepared_ids.append(job_id)
            return PreparedReply(job_id, 11, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert prepared_ids == ["reply:tg:tawg:11", "reply:tg:tawg:10"]


@pytest.mark.asyncio
async def test_reply_preparation_continues_after_one_job_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id=f"reply:tg:tawg:{message_id}",
            trigger_record_id=f"tg:tawg:{message_id}",
            reply_to_message_id=message_id,
            created_at=NOW,
            updated_at=NOW,
        )
        for message_id in range(10, 13)
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    attempted_ids: list[str] = []

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            attempted_ids.append(job_id)
            if job_id == "reply:tg:tawg:10":
                raise ReplyRejected("safe failure", safe_code="reply_test_failed")
            message_id = int(job_id.rsplit(":", 1)[-1])
            return PreparedReply(job_id, message_id, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert attempted_ids == [f"reply:tg:tawg:{message_id}" for message_id in range(10, 13)]
    assert [reply.job_id for reply in pipeline.prepared_replies] == [
        "reply:tg:tawg:11",
        "reply:tg:tawg:12",
    ]
    assert "tawg_event=phase_failed phase=reply_prepare code=reply_test_failed" in (
        capsys.readouterr().out
    )


@pytest.mark.asyncio
async def test_reply_preparation_budget_covers_three_realistic_sequential_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id=f"reply:tg:tawg:{message_id}",
            trigger_record_id=f"tg:tawg:{message_id}",
            reply_to_message_id=message_id,
            created_at=NOW,
            updated_at=NOW,
        )
        for message_id in range(10, 13)
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    monotonic_values = iter((100.0, 160.0, 461.0, 1001.0))
    attempted_ids: list[str] = []
    model_timeouts: list[float] = []

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            model_timeouts.append(kwargs["timeout_seconds"])

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            attempted_ids.append(job_id)
            message_id = int(job_id.rsplit(":", 1)[-1])
            return PreparedReply(job_id, message_id, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "monotonic", lambda: next(monotonic_values), raising=False)
    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert attempted_ids == [
        "reply:tg:tawg:10",
        "reply:tg:tawg:11",
        "reply:tg:tawg:12",
    ]
    assert model_timeouts == [300, 300, 299]


@pytest.mark.asyncio
async def test_daily_run_defers_new_pending_reply_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    job = PendingBotJob(
        job_id="reply:tg:tawg:10",
        trigger_record_id="tg:tawg:10",
        reply_to_message_id=10,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("Daily must not start another model job")

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.prepared_daily = PreparedDaily("daily", "Daily", ("Daily",), (), True)
        await pipeline._prepare_pending_replies()

    assert pipeline.prepared_replies == []


@pytest.mark.asyncio
async def test_failed_daily_attempt_defers_new_pending_reply_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    window = DailyWindow.for_due_run(NOW)
    job = PendingBotJob(
        job_id="reply:tg:tawg:10",
        trigger_record_id="tg:tawg:10",
        reply_to_message_id=10,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("failed Daily must not start another model job")

    async def reject_daily(*args: Any, **kwargs: Any) -> PreparedDaily:
        del args, kwargs
        raise DailyRejected("Daily output has an unexpected top-level section")

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        monkeypatch.setattr(pipeline, "prepare_daily", reject_daily)
        with pytest.raises(RuntimeFailure, match="Daily validation failed"):
            await pipeline.daily_prepare(window.window_id)
        await pipeline._prepare_pending_replies()

    assert pipeline.prepared_replies == []


@pytest.mark.asyncio
async def test_model_free_knowledge_phase_does_not_suppress_pending_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    job = PendingBotJob(
        job_id="reply:tg:tawg:10",
        trigger_record_id="tg:tawg:10",
        reply_to_message_id=10,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )

    prepared_calls: list[str] = []
    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> None:
            assert now == NOW
            prepared_calls.append(job_id)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline.knowledge_refresh(NOW)
        await pipeline._prepare_pending_replies()

    assert prepared_calls == [job.job_id]
    assert pipeline.prepared_replies == []


@pytest.mark.asyncio
async def test_script_checkpoint_reports_only_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"secret stdout", b"secret stderr"

    async def create(*args: Any, **kwargs: Any) -> Process:
        del args, kwargs
        return Process()

    monkeypatch.setattr(runtime_module.asyncio, "create_subprocess_exec", create)

    with pytest.raises(RuntimeFailure, match="checkpoint failed") as captured:
        await ScriptCheckpoint(tmp_path / "script.sh").publish("operation", tmp_path)
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_script_checkpoint_classifies_push_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        returncode = 75

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"secret stdout", b"secret stderr"

    async def create(*args: Any, **kwargs: Any) -> Process:
        del args, kwargs
        return Process()

    monkeypatch.setattr(runtime_module.asyncio, "create_subprocess_exec", create)

    with pytest.raises(RepositoryConflict) as captured:
        await ScriptCheckpoint(tmp_path / "script.sh").publish("operation", tmp_path)
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_runtime_fails_after_final_checkpoint_when_a_phase_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = Checkpoint()

    class Client:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            del args

    class Pipeline:
        def __init__(self, root: Path, **kwargs: object) -> None:
            del root, kwargs

    class Scheduler:
        def __init__(self, root: Path, *, pipeline: object) -> None:
            del root, pipeline

        async def tick(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return type(
                "Result",
                (),
                {
                    "layer": type("Layer", (), {"name": "L4"})(),
                    "failed_phases": ("daily_prepare",),
                },
            )()

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runtime_module, "_LivePipeline", Pipeline)
    monkeypatch.setattr(runtime_module, "Scheduler", Scheduler)

    with pytest.raises(RuntimeFailure, match="scheduled tick completed with phase failures"):
        await ProductionRuntime(tmp_path, checkpoint=checkpoint).tick(
            NOW, observe_only=False
        )

    assert checkpoint.operations == [f"layer-success:l4:{int(NOW.timestamp())}"]


@pytest.mark.asyncio
async def test_runtime_fails_after_checkpoint_when_a_reply_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = Checkpoint()

    class Client:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            del args

    class Pipeline:
        def __init__(self, root: Path, **kwargs: object) -> None:
            del root, kwargs
            self.reply_failures = ("reply:tg:tawg:10:reply_model_process_failed",)

    class Scheduler:
        def __init__(self, root: Path, *, pipeline: object) -> None:
            del root, pipeline

        async def tick(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return type(
                "Result",
                (),
                {
                    "layer": type("Layer", (), {"name": "L1"})(),
                    "failed_phases": (),
                },
            )()

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runtime_module, "_LivePipeline", Pipeline)
    monkeypatch.setattr(runtime_module, "Scheduler", Scheduler)

    with pytest.raises(RuntimeFailure, match="reply job failures"):
        await ProductionRuntime(tmp_path, checkpoint=checkpoint).tick(
            NOW, observe_only=False
        )

    assert checkpoint.operations == [f"layer-success:l1:{int(NOW.timestamp())}"]


@pytest.mark.asyncio
async def test_final_runtime_checkpoint_conflict_retries_in_a_fresh_repository_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_roots: list[Path] = []
    checkpoint_roots: list[Path] = []

    class Runner:
        async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult:
            del cwd
            if tuple(argv[:2]) == ("git", "clone"):
                Path(argv[-1]).mkdir()
            return CommandResult(returncode=0)

    class Client:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            del args

    class Pipeline:
        def __init__(self, root: Path, **kwargs: object) -> None:
            del root, kwargs

    class Scheduler:
        def __init__(self, root: Path, *, pipeline: object) -> None:
            del root, pipeline

        async def tick(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return type(
                "Result",
                (),
                {
                    "layer": type("Layer", (), {"name": "L1"})(),
                    "failed_phases": (),
                },
            )()

    class Process:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"private stdout", b"private stderr"

    async def create_checkpoint_process(*args: object, **kwargs: object) -> Process:
        assert args[0] == "bash"
        assert str(args[2]).startswith("layer-success:l1:")
        checkpoint_roots.append(Path(str(kwargs["cwd"])))
        return Process(75 if len(checkpoint_roots) == 1 else 0)

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runtime_module, "_LivePipeline", Pipeline)
    monkeypatch.setattr(runtime_module, "Scheduler", Scheduler)
    monkeypatch.setattr(
        runtime_module.asyncio,
        "create_subprocess_exec",
        create_checkpoint_process,
    )
    checkpoint = ScriptCheckpoint(Path("/safe/test-checkpoint.sh"))

    async def operation(root: Path) -> None:
        if operation_roots:
            assert not operation_roots[0].exists()
        operation_roots.append(root)
        await ProductionRuntime(root, checkpoint=checkpoint).maintenance_tick(
            NOW,
            observe_only=True,
        )

    await RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="main",
        runner=Runner(),
    ).run(operation_id="maintenance:retry", operation=operation)

    assert len(operation_roots) == 2
    assert operation_roots[0] != operation_roots[1]
    assert all(not root.exists() for root in operation_roots)
    assert checkpoint_roots == [root.resolve() for root in operation_roots]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["source", "daily"])
async def test_internal_checkpoint_conflict_retries_phase_in_a_fresh_repository_session(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_roots: list[Path] = []
    checkpoint_roots: list[Path] = []

    class Runner:
        async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult:
            del cwd
            if tuple(argv[:2]) == ("git", "clone"):
                Path(argv[-1]).mkdir()
            return CommandResult(returncode=0)

    class Process:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"private stdout", b"private stderr"

    async def create_checkpoint_process(*args: object, **kwargs: object) -> Process:
        assert args[0] == "bash"
        checkpoint_roots.append(Path(str(kwargs["cwd"])))
        return Process(75 if len(checkpoint_roots) == 1 else 0)

    monkeypatch.setattr(
        runtime_module.asyncio,
        "create_subprocess_exec",
        create_checkpoint_process,
    )
    checkpoint = ScriptCheckpoint(Path("/safe/test-checkpoint.sh"))

    async def operation(root: Path) -> None:
        if operation_roots:
            assert not operation_roots[0].exists()
        operation_roots.append(root)
        scaffold(root)
        async with httpx.AsyncClient() as client:
            if phase == "source":
                class Scanner:
                    async def scan(self, **kwargs: object) -> ScopedScanResult:
                        del kwargs
                        return ScopedScanResult(
                            observations=(),
                            github_cursors={},
                            magicians_cursors={},
                            failed_sources=(),
                        )

                    def stage(self, result: ScopedScanResult, uow: Any) -> None:
                        del result
                        uow.stage_json("data/state/scoped-source-observations.json", [])

                    def stage_repository_pages(
                        self,
                        uow: Any,
                        repositories: object,
                        now: datetime,
                    ) -> None:
                        del uow, repositories, now

                pipeline = _LivePipeline(root, client=client, checkpoint=checkpoint, now=NOW)
                pipeline.scoped_scanner = Scanner()  # type: ignore[assignment]
                pipeline.github_announcements = EmptyAnnouncements()  # type: ignore[assignment]
                await pipeline.source_check(NOW)
            else:
                class DailyPipeline(_LivePipeline):
                    async def prepare_daily(
                        self,
                        window: DailyWindow,
                        *,
                        dry_run: bool,
                    ) -> PreparedDaily:
                        assert not dry_run
                        return PreparedDaily(
                            window_id=window.window_id,
                            telegram_text="daily",
                            messages=("daily",),
                            citations=(),
                            quiet_day=False,
                        )

                pipeline = DailyPipeline(root, client=client, checkpoint=checkpoint, now=NOW)
                await pipeline.daily_prepare(DailyWindow.for_due_run(NOW).window_id)

    await RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="main",
        runner=Runner(),
    ).run(operation_id=f"{phase}:retry", operation=operation)

    assert len(operation_roots) == 2
    assert operation_roots[0] != operation_roots[1]
    assert all(not root.exists() for root in operation_roots)
    assert checkpoint_roots == [root.resolve() for root in operation_roots]


@pytest.mark.asyncio
async def test_webhook_runtime_checkpoints_before_bounded_threaded_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    seeded = tuple(
        webhook_envelope(
            update_id=index,
            message_id=99 + index,
            message_thread_id=199 + index,
        )
        for index in range(1, 11)
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=seeded,
        now=NOW,
    )
    incoming = webhook_envelope(update_id=11, message_id=110, message_thread_id=210)
    events: list[str] = []

    class IntakeCheckpoint(Checkpoint):
        async def publish(self, operation_id: str, root: Path) -> None:
            if operation_id == "telegram-webhook:11":
                receipts = json.loads(
                    (root / "data/state/telegram-webhook-receipts.json").read_text()
                )
                assert receipts["update_ids"][-1] == 11
                events.append("intake-checkpoint")
            await super().publish(operation_id, root)

    checkpoint = IntakeCheckpoint()

    class ReplyService:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            assert events == ["intake-checkpoint"]
            assert kwargs["bot_username"] == "tawg_bot"
            self.root = root

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            jobs = json.loads((self.root / "data/state/pending-bot-jobs.json").read_text())
            job = next(item for item in jobs if item["job_id"] == job_id)
            return PreparedReply(
                job_id,
                job["reply_to_message_id"],
                job["message_thread_id"],
                "reply",
                (),
                "en",
                False,
            )

    class Api:
        calls: ClassVar[list[tuple[int, int | None, int | None]]] = []

        @classmethod
        def from_env(cls, *, client: Any) -> Api:
            del client
            return cls()

        async def get_all_updates(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            raise AssertionError("webhook runtime must never poll getUpdates")

        async def send_message(
            self,
            chat_id: int,
            text: str,
            reply_to_message_id: int | None = None,
            message_thread_id: int | None = None,
        ) -> SentMessage:
            assert text == "reply"
            self.calls.append((chat_id, reply_to_message_id, message_thread_id))
            return SentMessage(message_id=1000 + len(self.calls), chat_id=chat_id)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setattr(runtime_module, "TelegramApi", Api)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-100123")

    result = await ProductionRuntime(tmp_path, checkpoint=checkpoint).ingest_webhook_envelope(
        incoming, now=NOW
    )

    assert result.persisted == 1
    assert len(Api.calls) == 10
    assert Api.calls == [
        (-100123, message_id, thread_id)
        for message_id, thread_id in zip(range(100, 110), range(200, 210), strict=True)
    ]
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert (
        next(item for item in jobs if item["job_id"] == "reply:tg:tawg:110")["status"] == "pending"
    )


@pytest.mark.asyncio
async def test_duplicate_webhook_replays_explicitly_failed_delivery_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    (tmp_path / "data/state/pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")
    incoming = webhook_envelope(update_id=50, message_id=500, message_thread_id=77)
    checkpoint = Checkpoint()

    class ReplyService:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del kwargs
            self.root = root

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            jobs = json.loads((self.root / "data/state/pending-bot-jobs.json").read_text())
            job = next(item for item in jobs if item["job_id"] == job_id)
            return PreparedReply(job_id, 500, job["message_thread_id"], "reply", (), "en", False)

    class Api:
        calls: ClassVar[list[tuple[int | None, int | None]]] = []

        @classmethod
        def from_env(cls, *, client: Any) -> Api:
            del client
            return cls()

        async def get_all_updates(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            raise AssertionError("webhook runtime must never poll getUpdates")

        async def send_message(
            self,
            chat_id: int,
            text: str,
            reply_to_message_id: int | None = None,
            message_thread_id: int | None = None,
        ) -> SentMessage:
            del text
            self.calls.append((reply_to_message_id, message_thread_id))
            if len(self.calls) == 1:
                raise TelegramApiError("explicit test rejection")
            return SentMessage(message_id=1234, chat_id=chat_id)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setattr(runtime_module, "TelegramApi", Api)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-100123")
    runtime = ProductionRuntime(tmp_path, checkpoint=checkpoint)

    first = await runtime.ingest_webhook_envelope(incoming, now=NOW)
    second = await runtime.ingest_webhook_envelope(incoming, now=NOW)

    assert first.persisted == 1
    assert second.replayed == 1
    assert Api.calls == [(500, 77), (500, 77)]
    attempts = json.loads((tmp_path / "data/state/delivery-state.json").read_text())
    attempt = next(item for item in attempts if item["job_id"] == "reply:tg:tawg:500")
    assert attempt["status"] == DeliveryStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_production_runtime_dispatches_new_manual_commands_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    events: list[str] = []

    class Client:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: Any) -> None:
            del args

    class Pipeline:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def check_sources(self, now: datetime, **kwargs: Any) -> SourceCheckSummary:
            del now
            events.append(f"check:{kwargs['erc_numbers']}:{kwargs['observe_only']}")
            return SourceCheckSummary(1, 2, 0, 1, not kwargs["observe_only"])

        async def refresh_knowledge(self, now: datetime, **kwargs: Any) -> RefreshResult:
            del now
            events.append(f"refresh:{kwargs['erc_numbers']}:{kwargs['dry_run']}")
            return RefreshResult((), (), False)

        async def publish_repository(self) -> None:
            events.append("repository")

        async def prepare_daily(self, window: DailyWindow, *, dry_run: bool) -> None:
            assert dry_run
            events.append(window.window_id)

    intake_policies: list[IntakePolicy] = []

    class Scheduler:
        def __init__(self, root: Path, *, pipeline: Any) -> None:
            del root, pipeline

        async def tick(
            self,
            now: datetime,
            *,
            observe_only: bool,
            intake_policy: IntakePolicy = IntakePolicy.POLL,
        ) -> Any:
            assert observe_only
            intake_policies.append(intake_policy)
            return type(
                "Result",
                (),
                {
                    "layer": type("Layer", (), {"name": "L1"})(),
                    "failed_phases": (),
                },
            )()

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runtime_module, "_LivePipeline", Pipeline)
    monkeypatch.setattr(runtime_module, "Scheduler", Scheduler)
    clock = type("Clock", (), {"now": staticmethod(lambda timezone: NOW)})
    monkeypatch.setattr(runtime_module, "datetime", clock)
    runtime = ProductionRuntime(tmp_path, checkpoint=checkpoint)

    await runtime.tick(NOW, observe_only=True)
    await runtime.maintenance_tick(NOW, observe_only=True)
    await runtime.check_sources(8004, observe_only=True)
    await runtime.refresh_knowledge(8183, dry_run=True)
    await runtime.daily_dry_run(DailyWindow.for_due_run(NOW).end)

    assert "check:(8004,):True" in events
    assert "refresh:frozenset({8183}):True" in events
    assert "repository" not in events
    assert intake_policies == [IntakePolicy.POLL, IntakePolicy.SKIP]
    assert any(item.startswith("layer-success:l1") for item in checkpoint.operations)


@pytest.mark.asyncio
async def test_daily_dry_run_uses_temporary_harness_and_leaves_repository_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    scaffold(root)
    (root / "data/state/delivery-state.json").write_text("[]\n", encoding="utf-8")
    fixture = json.loads((ROOT / "tests/fixtures/ai/daily-quiet.json").read_text())
    runtime_roots: list[Path] = []

    class Runner:
        async def run(self, *, argv: Any, **kwargs: Any) -> CompletedProcess:
            del kwargs
            policy = Path(argv[argv.index("--system-prompt-file") + 1])
            assert policy.is_file()
            outer = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "structured_output": fixture,
            }
            return CompletedProcess(0, json.dumps(outer).encode(), b"")

    def claude(*, root: Path, runtime_root: Path) -> RealClaudeCli:
        assert not runtime_root.resolve().is_relative_to(root.resolve())
        runtime_roots.append(runtime_root)
        return RealClaudeCli(root=root, runner=Runner(), runtime_root=runtime_root)

    class Collector:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def collect(self, window: DailyWindow, *, now: datetime) -> tuple:
            assert window == DailyWindow.for_due_run(NOW)
            assert now == NOW
            return ()

    def snapshot() -> tuple[tuple[str, bool, bytes | None], ...]:
        return tuple(
            (
                path.relative_to(root).as_posix(),
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in sorted(root.rglob("*"))
        )

    monkeypatch.setattr(runtime_module, "ClaudeCli", claude)
    monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
    clock = type("Clock", (), {"now": staticmethod(lambda timezone: NOW)})
    monkeypatch.setattr(runtime_module, "datetime", clock)
    before = snapshot()

    prepared = await ProductionRuntime(root, checkpoint=Checkpoint()).daily_dry_run(
        DailyWindow.for_due_run(NOW).end
    )

    assert prepared is not None
    assert prepared.quiet_day
    assert snapshot() == before
    assert runtime_roots
    assert all(not path.exists() for path in runtime_roots)


@pytest.mark.asyncio
async def test_knowledge_dry_run_uses_temporary_harness_and_leaves_repository_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    scaffold(root)
    runtime_roots: list[Path] = []
    structured = {
        "schema_version": "tawg.knowledge-result.v2",
        "processed_job_keys": ["refresh:test"],
        "evidence_gaps": [],
        "transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": "knowledge-refresh-test",
            "writes": [
                {
                    "path": "knowledge/ercs/erc-8004.md",
                    "expected_sha256": None,
                    "content": "Generated preview",
                    "citations": [],
                }
            ],
        },
    }

    class Runner:
        async def run(self, *, argv: Any, **kwargs: Any) -> CompletedProcess:
            del kwargs
            policy = Path(argv[argv.index("--system-prompt-file") + 1])
            assert policy.is_file()
            outer = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "structured_output": structured,
            }
            return CompletedProcess(0, json.dumps(outer).encode(), b"")

    def claude(*, root: Path, runtime_root: Path) -> RealClaudeCli:
        assert not runtime_root.resolve().is_relative_to(root.resolve())
        runtime_roots.append(runtime_root)
        return RealClaudeCli(root=root, runner=Runner(), runtime_root=runtime_root)

    class Refresh:
        def __init__(self, root: Path, *, ai: Any, **kwargs: Any) -> None:
            del root, kwargs
            self.ai = ai

        async def run(self, **kwargs: Any) -> RefreshResult:
            assert kwargs["dry_run"] is True
            assert kwargs["operation_id"] == "knowledge-refresh-20260824t011700z"
            await self.ai.run(
                job_type="knowledge",
                context_pack='{"safe":"preview"}',
                operation_id="knowledge-refresh-test",
                max_budget_usd="0.10",
            )
            return RefreshResult(("refresh:test",), ("knowledge/ercs/erc-8004.md",), False)

    def snapshot() -> tuple[tuple[str, bool, bytes | None], ...]:
        return tuple(
            (
                path.relative_to(root).as_posix(),
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in sorted(root.rglob("*"))
        )

    monkeypatch.setattr(runtime_module, "ClaudeCli", claude)
    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
    clock = type("Clock", (), {"now": staticmethod(lambda timezone: NOW)})
    monkeypatch.setattr(runtime_module, "datetime", clock)
    before = snapshot()

    result = await ProductionRuntime(root, checkpoint=Checkpoint()).refresh_knowledge(
        8004, dry_run=True
    )

    assert result.processed_job_keys == ("refresh:test",)
    assert snapshot() == before
    assert runtime_roots
    assert all(not path.exists() for path in runtime_roots)
