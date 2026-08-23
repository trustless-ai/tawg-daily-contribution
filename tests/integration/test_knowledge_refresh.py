from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.knowledge_refresh import KnowledgeRefresh
from tawg_bot.source_registry import SourceRegistry

PROJECT = Path(__file__).parents[2]
CUTOFF = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


class NeverAi:
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("AI must not run without a queued knowledge job")


class NeverLiveEvidence:
    async def build(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("live evidence must not run without a queued knowledge job")


@pytest.mark.asyncio
async def test_refresh_without_queued_jobs_is_a_noop(tmp_path: Path) -> None:
    for relative in ("config/privacy.yml", "knowledge/meta/sources.yml"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    for name in (
        "knowledge-gaps.json",
        "pending-knowledge-refresh.json",
        "source-candidates.json",
    ):
        (state / name).write_text("[]\n", encoding="utf-8")
    registry = SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml")

    result = await KnowledgeRefresh(
        tmp_path,
        ai=NeverAi(),
        live_evidence=NeverLiveEvidence(),
        registry=registry,
    ).run(cutoff=CUTOFF, operation_id="no-jobs")

    assert result.processed_job_keys == ()
    assert result.changed_paths == ()
    assert not result.index_rebuilt
