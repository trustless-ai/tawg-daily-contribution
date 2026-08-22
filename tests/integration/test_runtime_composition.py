from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.daily import DailyWindow, PreparedDaily
from tawg_bot.github_source import GitHubBatch
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.runtime import ProductionRuntime, RuntimeFailure, ScriptCheckpoint, _LivePipeline

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


def scaffold(root: Path) -> None:
    for relative in ("config", "knowledge", "prompts", "bot-skill", "src/tawg_bot/schemas"):
        shutil.copytree(ROOT / relative, root / relative)
    state = root / "data/state"
    state.mkdir(parents=True)
    for source in (ROOT / "data/state").iterdir():
        if source.is_file():
            (state / source.name).write_bytes(source.read_bytes())


def record(record_id: str, source_type: SourceType, text: str) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=source_type,
        source_locator="https://github.com/trustless-ai/agent-ercs/commit/abc",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
        text_original=text,
        ingested_at=NOW,
        source_payload={},
    )


@pytest.mark.asyncio
async def test_live_pipeline_stages_sources_artifacts_and_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path, client=client, checkpoint=checkpoint, now=NOW
        )
        github_record = record(
            "gh:agent-ercs:commit:abc", SourceType.GITHUB_COMMIT, "ERC-8004 update"
        )
        pipeline._stage_github(
            GitHubBatch(records=(github_record,), cursors={"agent-ercs:x": "y"})
        )
        pipeline.cursors.github = {"agent-ercs:x": "y"}

        await pipeline.publish_sources()

        assert checkpoint.operations == [f"sources:{int(NOW.timestamp())}"]
        assert (tmp_path / "data/github/agent-ercs/2026/08/records.jsonl").is_file()
        assert pipeline._source_config()["github"]["organization"] == "trustless-ai"
        assert pipeline._topic_id("https://ethereum-magicians.org/t/name/25098") == 25098
        assert pipeline._topic_id("https://ethereum-magicians.org/nope") is None
        assert pipeline._string_set(["a", "b"]) == {"a", "b"}
        with pytest.raises(RuntimeFailure):
            pipeline._string_set([1])
        assert pipeline._load_candidates() == []
        assert pipeline._load_jobs() == []
        assert any(
            item.record_id == github_record.record_id
            for item in pipeline._all_source_records()
        )

        class Refresh:
            def __init__(self, root: Path, *, ai: Any) -> None:
                del root, ai

            async def run(self, **kwargs: Any) -> None:
                assert kwargs["cutoff"] == NOW

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

            async def prepare(self, window: DailyWindow, *, readiness: Any) -> PreparedDaily:
                assert window.window_id == prepared.window_id
                assert readiness.knowledge_refreshed_at == NOW
                return prepared

        monkeypatch.setattr(runtime_module, "DailyService", Daily)
        pipeline.telegram_synced_at = NOW
        pipeline.github_synced_at = NOW
        pipeline.magicians_synced_at = NOW
        await pipeline.prepare_daily(DailyWindow.for_due_run(NOW), dry_run=True)
        assert json.loads(
            (tmp_path / "data/state/prepared-daily.json").read_text()
        )["dry_run"]

        async def no_replies() -> None:
            pipeline.prepared_replies = []

        monkeypatch.setattr(pipeline, "_prepare_pending_replies", no_replies)
        await pipeline.publish_repository()
        assert checkpoint.operations[-1].startswith("prepared:")


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
async def test_production_runtime_dispatches_tick_backfill_and_dry_run(
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

        async def github_sync(self, now: datetime) -> None:
            del now
            events.append("github")

        async def magicians_sync(self, now: datetime) -> None:
            del now
            events.append("magicians")

        async def publish_sources(self) -> None:
            events.append("sources")

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
    await runtime.backfill("github")
    await runtime.backfill("magicians")
    await runtime.daily_dry_run(DailyWindow.for_due_run(NOW).end)
    with pytest.raises(ValueError):
        await runtime.backfill("other")

    assert {"github", "magicians", "sources", "repository"}.issubset(events)
    assert any(item.startswith("layer-success:l1") for item in checkpoint.operations)
