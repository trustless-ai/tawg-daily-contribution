from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.models import LayerSuccess
from tawg_bot.scheduler import Scheduler
from tests.unit.test_scheduler import Pipeline

NOW = datetime(2026, 8, 23, 23, 5, tzinfo=UTC)


def service(root: Path, pipeline: Pipeline) -> Scheduler:
    state = root / "data/state"
    state.mkdir(parents=True)
    old = NOW - timedelta(days=2)
    (state / "layer-success.json").write_text(
        LayerSuccess(l1=old, l2=old, l3=old, l4=old).model_dump_json(indent=2) + "\n"
    )
    return Scheduler(root, pipeline=pipeline)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["github", "magicians", "validate"])
async def test_required_source_or_validator_failure_skips_daily_and_delivery(
    tmp_path: Path, failure: str
) -> None:
    pipeline = Pipeline(fail_at=failure)

    with pytest.raises(RuntimeError):
        await service(tmp_path, pipeline).tick(NOW)

    assert "delivery" not in pipeline.events
    if failure != "validate":
        assert "daily" not in pipeline.events


@pytest.mark.asyncio
async def test_l4_uses_fixed_latest_due_window_id(tmp_path: Path) -> None:
    class WindowPipeline(Pipeline):
        async def daily_prepare(self, window_id: str) -> None:
            self.events.append(window_id)

    pipeline = WindowPipeline()

    await service(tmp_path, pipeline).tick(datetime(2026, 8, 24, 1, 17, tzinfo=UTC))

    assert "daily:2026-08-23T23:00:00Z" in pipeline.events
