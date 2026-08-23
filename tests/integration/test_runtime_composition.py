from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.claude_cli import ClaudeCli as RealClaudeCli
from tawg_bot.claude_cli import CompletedProcess
from tawg_bot.daily import DailyWindow, PreparedDaily
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_refresh import RefreshResult
from tawg_bot.live_evidence import LiveEvidenceService
from tawg_bot.runtime import (
    ProductionRuntime,
    RuntimeFailure,
    ScriptCheckpoint,
    SourceCheckSummary,
    _LivePipeline,
)
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
    async def build(self, query, *, now):
        assert now == NOW
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
            ) -> None:
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
            def __init__(self, root: Path, *, ai: Any) -> None:
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
                del root, kwargs

            async def collect(self, window: DailyWindow, *, now: datetime) -> tuple:
                assert window.window_id == prepared.window_id
                assert now == NOW
                return ()

        monkeypatch.setattr(runtime_module, "DailyService", Daily)
        monkeypatch.setattr(runtime_module, "DailyEvidenceCollector", Collector)
        pipeline.telegram_synced_at = NOW
        pipeline.source_checked_at = NOW
        await pipeline.prepare_daily(DailyWindow.for_due_run(NOW), dry_run=True)
        assert pipeline.prepared_daily == prepared
        assert not (tmp_path / "data/state/prepared-daily.json").exists()

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
