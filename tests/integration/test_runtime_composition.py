from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.bot_router import PreparedReply
from tawg_bot.claude_cli import ClaudeCli as RealClaudeCli
from tawg_bot.claude_cli import CompletedProcess
from tawg_bot.daily import DailyRejected, DailyWindow, PreparedDaily
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_refresh import RefreshResult
from tawg_bot.live_evidence import LiveEvidenceService
from tawg_bot.models import PendingBotJob
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.runtime import (
    ProductionRuntime,
    RuntimeFailure,
    ScriptCheckpoint,
    SourceCheckSummary,
    _LivePipeline,
)
from tawg_bot.source_registry import EvidenceKind, SourceRegistry
from tests.integration.test_live_knowledge_refresh import _pack

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


class LiveEvidence:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def build(self, query, *, now):
        assert now == NOW
        self.calls.append(query)
        return _pack().model_copy(update={"query": query})


def scaffold(root: Path) -> None:
    for relative in (
        "config",
        "knowledge",
        "prompts",
        "bot-skill",
        "src/tawg_bot/schemas",
    ):
        shutil.copytree(ROOT / relative, root / relative)
    state = root / "data/state"
    state.mkdir(parents=True)
    for source in (ROOT / "data/state").iterdir():
        if source.is_file():
            (state / source.name).write_bytes(source.read_bytes())


@pytest.mark.asyncio
async def test_live_pipeline_checks_sources_without_external_body_mirrors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        assert isinstance(pipeline.live_evidence, LiveEvidenceService)
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
                assert timeout_seconds == 360
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
async def test_scheduled_source_check_skips_fresh_registered_sources(tmp_path: Path) -> None:
    scaffold(tmp_path)
    registry = SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml")
    observations = {
        source.source_key: source.last_observed.model_copy(update={"checked_at": NOW})
        for erc in registry.erc_numbers()
        for source in registry.resolve(erc, frozenset(EvidenceKind))
        if source.last_observed is not None
    }
    (tmp_path / "knowledge/meta/sources.yml").write_text(
        registry.render_with_observations(observations),
        encoding="utf-8",
    )
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        live = LiveEvidence()
        pipeline.live_evidence = live

        await pipeline.source_check(NOW)

    assert live.calls == []
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
async def test_scheduled_source_batches_checkpoint_success_and_skip_safe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    calls: list[tuple[int, ...]] = []
    timeouts: list[float] = []
    original_wait_for = asyncio.wait_for

    async def bounded(awaitable: Any, *, timeout: float) -> Any:
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(runtime_module.asyncio, "wait_for", bounded)

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        monkeypatch.setattr(
            pipeline.registry,
            "due_erc_numbers",
            lambda now, max_age: (8004, 8183, 8263),
        )

        async def check_sources(
            now: datetime, *, erc_numbers: tuple[int, ...], observe_only: bool
        ) -> SourceCheckSummary:
            assert now == NOW
            assert not observe_only
            calls.append(erc_numbers)
            if erc_numbers == (8183,):
                raise RuntimeError("provider-secret-body")
            return SourceCheckSummary(1, 2, 0, 1, True)

        monkeypatch.setattr(pipeline, "check_sources", check_sources)
        with pytest.raises(RuntimeFailure, match="source check incomplete"):
            await pipeline.source_check(NOW)

    assert calls == [(8004,), (8183,)]
    assert timeouts == [60, 60]
    assert checkpoint.operations == [f"source-check:erc-8004:{int(NOW.timestamp())}"]
    captured = capsys.readouterr().out
    assert "erc=8183" in captured
    assert "code=source_check_failed" in captured
    assert "provider-secret-body" not in captured


@pytest.mark.asyncio
async def test_scheduled_knowledge_refresh_uses_one_erc_and_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    calls: list[dict[str, Any]] = []

    class State:
        def eligible_refresh_erc_numbers(self, cutoff: datetime) -> tuple[int, ...]:
            assert cutoff == NOW
            return (8004, 8183)

    class Refresh:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            assert root == tmp_path
            assert kwargs["max_ercs_per_run"] == 1
            assert kwargs["timeout_seconds"] == 180

        async def run(self, **kwargs: Any) -> RefreshResult:
            calls.append(kwargs)
            return RefreshResult(("refresh:8004",), ("knowledge/ercs/erc-8004.md",), True)

    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        pipeline.knowledge_state = State()  # type: ignore[assignment]
        await pipeline.knowledge_refresh(NOW)

    assert calls[0]["erc_numbers"] == frozenset({8004})
    assert checkpoint.operations == [f"knowledge-refresh:erc-8004:{int(NOW.timestamp())}"]


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
async def test_scheduled_knowledge_failure_is_deferred_and_logged_without_raw_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    deferred: list[tuple[int, str]] = []

    class State:
        def eligible_refresh_erc_numbers(self, cutoff: datetime) -> tuple[int, ...]:
            del cutoff
            return (8004,)

        def defer_refresh_erc(
            self,
            uow: Any,
            erc_number: int,
            *,
            now: datetime,
            safe_error_code: str,
        ) -> None:
            assert now == NOW
            deferred.append((erc_number, safe_error_code))

    class Refresh:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def run(self, **kwargs: Any) -> RefreshResult:
            del kwargs
            raise RuntimeError("model-secret-output")

    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        pipeline.knowledge_state = State()  # type: ignore[assignment]
        with pytest.raises(RuntimeFailure, match="knowledge refresh deferred"):
            await pipeline.knowledge_refresh(NOW)

    assert deferred == [(8004, "knowledge_refresh_failed")]
    assert checkpoint.operations == [f"knowledge-deferred:erc-8004:{int(NOW.timestamp())}"]
    captured = capsys.readouterr().out
    assert "code=knowledge_refresh_failed" in captured
    assert "model-secret-output" not in captured


@pytest.mark.asyncio
async def test_successful_knowledge_is_not_deferred_when_only_checkpoint_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)

    class CheckpointFailure:
        async def publish(self, operation_id: str, root: Path) -> None:
            del operation_id, root
            raise RuntimeError("push-secret-output")

    class State:
        def eligible_refresh_erc_numbers(self, cutoff: datetime) -> tuple[int, ...]:
            del cutoff
            return (8004,)

        def defer_refresh_erc(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("successful refresh must not be deferred")

    class Refresh:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            del root, kwargs

        async def run(self, **kwargs: Any) -> RefreshResult:
            del kwargs
            return RefreshResult(("refresh:8004",), ("knowledge/ercs/erc-8004.md",), True)

    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", Refresh)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=CheckpointFailure(),
            now=NOW,
        )
        pipeline.knowledge_state = State()  # type: ignore[assignment]
        with pytest.raises(RuntimeFailure, match="knowledge checkpoint incomplete"):
            await pipeline.knowledge_refresh(NOW)

    captured = capsys.readouterr().out
    assert "code=knowledge_checkpoint_failed" in captured
    assert "push-secret-output" not in captured


@pytest.mark.asyncio
async def test_reply_preparation_processes_only_one_pending_job_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    jobs = [
        PendingBotJob(
            job_id=f"reply:tg:tawg:{message_id}",
            trigger_record_id=f"tg:tawg:{message_id}",
            reply_to_message_id=message_id,
            created_at=NOW,
            updated_at=NOW if message_id == 10 else NOW - timedelta(minutes=1),
        )
        for message_id in (10, 11)
    ]
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    prepared_ids: list[str] = []

    class ReplyService:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            assert root == tmp_path
            assert kwargs["timeout_seconds"] == 120

        async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
            assert now == NOW
            prepared_ids.append(job_id)
            return PreparedReply(job_id, 10, None, "reply", (), "en", False)

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline._prepare_pending_replies()

    assert prepared_ids == ["reply:tg:tawg:11"]


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

    class Scheduler:
        def __init__(self, root: Path, *, pipeline: Any) -> None:
            del root, pipeline

        async def tick(self, now: datetime, *, observe_only: bool) -> Any:
            assert observe_only
            return type("Result", (), {"layer": type("Layer", (), {"name": "L1"})()})()

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runtime_module, "_LivePipeline", Pipeline)
    monkeypatch.setattr(runtime_module, "Scheduler", Scheduler)
    clock = type("Clock", (), {"now": staticmethod(lambda timezone: NOW)})
    monkeypatch.setattr(runtime_module, "datetime", clock)
    runtime = ProductionRuntime(tmp_path, checkpoint=checkpoint)

    await runtime.tick(NOW, observe_only=True)
    await runtime.check_sources(8004, observe_only=True)
    await runtime.refresh_knowledge(8183, dry_run=True)
    await runtime.daily_dry_run(DailyWindow.for_due_run(NOW).end)

    assert "check:(8004,):True" in events
    assert "refresh:frozenset({8183}):True" in events
    assert "repository" not in events
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
