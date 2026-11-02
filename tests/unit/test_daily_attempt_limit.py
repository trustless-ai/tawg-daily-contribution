from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.daily import DailyWindow
from tawg_bot.daily_attempt_limit import DailyAttemptLimit, DailyAttemptsExhausted
from tawg_bot.models import LayerSuccess
from tawg_bot.scheduler import Layer, Scheduler


class Checkpoint:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def publish(self, operation_id: str, root: Path) -> None:
        self.calls += 1
        assert (
            json.loads((root / "data/state/daily-attempt-limit.json").read_text())["attempts"]
            == self.calls
        )
        if self.fail:
            raise RuntimeError("checkpoint failed")


class FailingAi:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("upstream failed")


@pytest.mark.asyncio
async def test_limit_survives_recreated_worker_and_new_window_resets(tmp_path: Path) -> None:
    ai = FailingAi()
    checkpoint = Checkpoint()
    window = "daily:2026-08-23T23:00:00Z"
    for _ in range(3):
        guard = DailyAttemptLimit(tmp_path, window_id=window, checkpoint=checkpoint, ai=ai)
        with pytest.raises(RuntimeError, match="upstream failed"):
            await guard.run(
                job_type="daily",
                context_pack="{}",
                operation_id="test",
                max_budget_usd="2",
                timeout_seconds=10,
            )
    guard = DailyAttemptLimit(tmp_path, window_id=window, checkpoint=checkpoint, ai=ai)
    with pytest.raises(DailyAttemptsExhausted):
        await guard.run(
            job_type="daily",
            context_pack="{}",
            operation_id="test",
            max_budget_usd="2",
            timeout_seconds=10,
        )
    assert ai.calls == 3
    assert DailyAttemptLimit.exhausted(tmp_path, window)
    assert not DailyAttemptLimit.exhausted(tmp_path, "daily:2026-08-24T23:00:00Z")


@pytest.mark.asyncio
async def test_checkpoint_failure_prevents_model_call(tmp_path: Path) -> None:
    ai = FailingAi()
    guard = DailyAttemptLimit(
        tmp_path, window_id="daily:2026-08-23T23:00:00Z", checkpoint=Checkpoint(fail=True), ai=ai
    )
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await guard.run(
            job_type="daily",
            context_pack="{}",
            operation_id="test",
            max_budget_usd="2",
            timeout_seconds=10,
        )
    assert ai.calls == 0


def test_exhausted_daily_does_not_block_other_scheduled_work(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    (state / "layer-success.json").write_text(
        LayerSuccess(
            l1=now, l2=now - timedelta(hours=1), l3=now, l4=now - timedelta(days=2)
        ).model_dump_json()
    )
    window = DailyWindow.for_due_run(now).window_id
    (state / "daily-attempt-limit.json").write_text(
        json.dumps(
            {"schema_version": "tawg.daily-attempt-limit.v1", "window_id": window, "attempts": 3}
        )
    )
    service = Scheduler(tmp_path, pipeline=None)  # type: ignore[arg-type]
    assert service.due_layer(now) is Layer.L2
    assert service.due_layer(now + timedelta(days=1)) is Layer.L4


@pytest.mark.asyncio
async def test_next_window_can_call_model_after_prior_window_is_exhausted(tmp_path: Path) -> None:
    path = tmp_path / "data/state/daily-attempt-limit.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "tawg.daily-attempt-limit.v1",
                "window_id": "daily:2026-08-23T23:00:00Z",
                "attempts": 3,
            }
        )
    )
    ai = FailingAi()
    guard = DailyAttemptLimit(
        tmp_path, window_id="daily:2026-08-24T23:00:00Z", checkpoint=Checkpoint(), ai=ai
    )
    with pytest.raises(RuntimeError, match="upstream failed"):
        await guard.run(
            job_type="daily",
            context_pack="{}",
            operation_id="test",
            max_budget_usd="2",
            timeout_seconds=10,
        )
    assert ai.calls == 1
    assert json.loads(path.read_text())["window_id"] == "daily:2026-08-24T23:00:00Z"


@pytest.mark.asyncio
async def test_corrupt_state_prevents_model_call(tmp_path: Path) -> None:
    path = tmp_path / "data/state/daily-attempt-limit.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json")
    ai = FailingAi()
    guard = DailyAttemptLimit(
        tmp_path, window_id="daily:2026-08-23T23:00:00Z", checkpoint=Checkpoint(), ai=ai
    )
    with pytest.raises(ValueError, match="invalid Daily attempt state"):
        await guard.run(
            job_type="daily",
            context_pack="{}",
            operation_id="test",
            max_budget_usd="2",
            timeout_seconds=10,
        )
    assert ai.calls == 0


@pytest.mark.asyncio
async def test_checkpoint_conflict_never_runs_model(tmp_path: Path) -> None:
    from tawg_bot.repository_session import RepositoryConflict

    class ConflictCheckpoint:
        async def publish(self, operation_id: str, root: Path) -> None:
            raise RepositoryConflict

    ai = FailingAi()
    guard = DailyAttemptLimit(
        tmp_path, window_id="daily:2026-08-23T23:00:00Z", checkpoint=ConflictCheckpoint(), ai=ai
    )
    with pytest.raises(RepositoryConflict):
        await guard.run(
            job_type="daily",
            context_pack="{}",
            operation_id="test",
            max_budget_usd="2",
            timeout_seconds=10,
        )
    assert ai.calls == 0
