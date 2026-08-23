from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.cli import _parser, main
from tawg_bot.daily import PreparedDaily
from tawg_bot.models import LayerSuccess
from tawg_bot.scheduler import Layer, Scheduler

NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)


class Pipeline:
    def __init__(self, fail_at: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at

    async def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"failed: {name}")

    async def telegram_intake(self, now: datetime) -> None:
        del now
        await self._event("telegram")

    async def source_check(self, now: datetime) -> None:
        del now
        await self._event("source_check")

    async def knowledge_refresh(self, cutoff: datetime) -> None:
        del cutoff
        await self._event("knowledge")

    async def validate(self) -> None:
        await self._event("validate")

    async def daily_prepare(self, window_id: str) -> None:
        del window_id
        await self._event("daily")

    async def publish_repository(self) -> None:
        await self._event("repo_publish")

    async def telegram_delivery(self) -> None:
        await self._event("delivery")


def scheduler(root: Path, pipeline: Pipeline, success: LayerSuccess) -> Scheduler:
    state = root / "data/state"
    state.mkdir(parents=True)
    (state / "layer-success.json").write_text(success.model_dump_json(indent=2) + "\n")
    return Scheduler(root, pipeline=pipeline)


@pytest.mark.parametrize(
    ("success", "expected"),
    [
        (LayerSuccess(l1=NOW, l2=NOW, l3=NOW, l4=NOW), Layer.L1),
        (
            LayerSuccess(
                l1=NOW,
                l2=NOW - timedelta(minutes=31),
                l3=NOW,
                l4=NOW,
            ),
            Layer.L2,
        ),
        (
            LayerSuccess(
                l1=NOW,
                l2=NOW,
                l3=NOW - timedelta(hours=2, seconds=1),
                l4=NOW,
            ),
            Layer.L3,
        ),
        (
            LayerSuccess(l1=NOW, l2=NOW, l3=NOW, l4=datetime(2026, 8, 22, 23, tzinfo=UTC)),
            Layer.L4,
        ),
    ],
)
def test_due_layer_uses_durable_success_times(
    tmp_path: Path, success: LayerSuccess, expected: Layer
) -> None:
    assert scheduler(tmp_path, Pipeline(), success).due_layer(NOW) is expected


@pytest.mark.asyncio
async def test_every_tick_runs_l1_first_and_heavier_layer_includes_earlier_work(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline()
    old = NOW - timedelta(days=2)
    service = scheduler(tmp_path, pipeline, LayerSuccess(l1=old, l2=old, l3=old, l4=old))

    result = await service.tick(NOW)

    assert result.layer is Layer.L4
    assert pipeline.events == [
        "telegram",
        "source_check",
        "knowledge",
        "validate",
        "daily",
        "repo_publish",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_failed_layer_keeps_old_success_state(tmp_path: Path) -> None:
    old = NOW - timedelta(days=2)
    initial = LayerSuccess(l1=old, l2=old, l3=old, l4=old)
    pipeline = Pipeline(fail_at="source_check")
    service = scheduler(tmp_path, pipeline, initial)

    with pytest.raises(RuntimeError, match="source_check"):
        await service.tick(NOW)

    assert service.load_success() == initial
    assert pipeline.events == ["telegram", "source_check"]


@pytest.mark.asyncio
async def test_observe_only_keeps_daily_window_retryable(tmp_path: Path) -> None:
    old = NOW - timedelta(days=2)
    pipeline = Pipeline()
    service = scheduler(tmp_path, pipeline, LayerSuccess(l1=old, l2=old, l3=old, l4=old))

    await service.tick(NOW, observe_only=True)

    assert "delivery" not in pipeline.events
    assert service.load_success().l4 == old


def test_cli_exposes_scheduled_and_manual_operator_commands() -> None:
    parser = _parser()

    assert (
        parser.parse_args(["tick", "--now", "2026-08-23T23:00:00Z", "--observe-only"]).command
        == "tick"
    )
    checked = parser.parse_args(["check-sources", "--erc", "8004", "--observe-only"])
    assert checked.erc == 8004
    assert checked.observe_only
    refreshed = parser.parse_args(["refresh-knowledge", "--erc", "8183", "--dry-run"])
    assert refreshed.erc == 8183
    assert refreshed.dry_run
    assert (
        parser.parse_args(["daily-dry-run", "--window-end", "2026-08-23T23:00:00Z"]).command
        == "daily-dry-run"
    )
    assert parser.parse_args(["vault-lint"]).command == "vault-lint"


def test_daily_dry_run_prints_the_prepared_message(capsys: pytest.CaptureFixture[str]) -> None:
    class Runtime:
        async def daily_dry_run(self, window_end: datetime) -> PreparedDaily:
            assert window_end == datetime(2026, 8, 23, 23, tzinfo=UTC)
            return PreparedDaily(
                window_id="daily:2026-08-23T23:00:00Z",
                telegram_text="Friendly current Daily",
                messages=("Friendly current Daily",),
                citations=(),
                quiet_day=False,
            )

    assert (
        main(
            ["daily-dry-run", "--window-end", "2026-08-23T23:00:00Z"],
            runtime=Runtime(),  # type: ignore[arg-type]
        )
        == 0
    )
    assert capsys.readouterr().out == "Friendly current Daily\n"
