from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.cli import _parser, main
from tawg_bot.daily import PreparedDaily
from tawg_bot.models import LayerSuccess
from tawg_bot.repository_session import RepositoryConflict
from tawg_bot.scheduler import IntakePolicy, Layer, Scheduler

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

    async def maybe_ask_busy_question(self, now: datetime) -> None:
        del now
        await self._event("busy_question")


def scheduler(root: Path, pipeline: Pipeline, success: LayerSuccess) -> Scheduler:
    state = root / "data/state"
    state.mkdir(parents=True, exist_ok=True)
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
async def test_daily_tick_prioritizes_current_daily_over_deferred_heavy_work(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline()
    old = NOW - timedelta(days=2)
    service = scheduler(tmp_path, pipeline, LayerSuccess(l1=old, l2=old, l3=old, l4=old))

    result = await service.tick(NOW)

    assert result.layer is Layer.L4
    assert pipeline.events == [
        "telegram",
        "validate",
        "daily",
        "repo_publish",
        "busy_question",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_failed_source_is_logged_safely_and_later_work_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = NOW - timedelta(days=2)
    initial = LayerSuccess(l1=old, l2=old, l3=NOW, l4=NOW)
    pipeline = Pipeline(fail_at="source_check")
    service = scheduler(tmp_path, pipeline, initial)

    result = await service.tick(NOW)

    success = service.load_success()
    assert success.l1 == NOW
    assert success.l2 == old
    assert pipeline.events == [
        "telegram",
        "source_check",
        "repo_publish",
        "busy_question",
        "delivery",
    ]
    captured = capsys.readouterr().out
    assert "phase=source_check" in captured
    assert "code=source_check_failed" in captured
    assert "failed: source_check" not in captured
    assert result.failed_phases == ("source_check",)


@pytest.mark.asyncio
async def test_repository_conflict_escapes_scheduler_phase_for_outer_retry(
    tmp_path: Path,
) -> None:
    class ConflictPipeline(Pipeline):
        async def publish_repository(self) -> None:
            self.events.append("repo_publish")
            raise RepositoryConflict

    old = NOW - timedelta(days=2)
    pipeline = ConflictPipeline()
    service = scheduler(
        tmp_path,
        pipeline,
        LayerSuccess(l1=old, l2=NOW, l3=NOW, l4=NOW),
    )

    with pytest.raises(RepositoryConflict):
        await service.tick(NOW)

    assert pipeline.events == ["telegram", "repo_publish"]


@pytest.mark.asyncio
async def test_failed_daily_stays_retryable_but_replies_can_still_deliver(
    tmp_path: Path,
) -> None:
    old = NOW - timedelta(days=2)
    pipeline = Pipeline(fail_at="daily")
    service = scheduler(tmp_path, pipeline, LayerSuccess(l1=old, l2=NOW, l3=NOW, l4=old))

    result = await service.tick(NOW)

    assert not result.delivered
    assert service.load_success().l4 == old
    assert pipeline.events == [
        "telegram",
        "validate",
        "daily",
        "repo_publish",
        "busy_question",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_l2_and_l3_runs_each_do_only_their_bounded_heavy_phase(tmp_path: Path) -> None:
    old = NOW - timedelta(days=2)
    l2_pipeline = Pipeline()
    l2 = scheduler(
        tmp_path,
        l2_pipeline,
        LayerSuccess(l1=old, l2=old, l3=NOW, l4=NOW),
    )
    await l2.tick(NOW)
    assert l2_pipeline.events == [
        "telegram",
        "source_check",
        "repo_publish",
        "busy_question",
        "delivery",
    ]

    l3_pipeline = Pipeline()
    l3 = scheduler(
        tmp_path,
        l3_pipeline,
        LayerSuccess(l1=old, l2=NOW, l3=old, l4=NOW),
    )
    await l3.tick(NOW)
    assert l3_pipeline.events == [
        "telegram",
        "knowledge",
        "validate",
        "repo_publish",
        "busy_question",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_observe_only_keeps_daily_window_retryable(tmp_path: Path) -> None:
    old = NOW - timedelta(days=2)
    pipeline = Pipeline()
    service = scheduler(tmp_path, pipeline, LayerSuccess(l1=old, l2=old, l3=old, l4=old))

    await service.tick(NOW, observe_only=True)

    assert "delivery" not in pipeline.events
    assert service.load_success().l4 == old


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "expected_events"),
    [
        (
            LayerSuccess(l1=NOW, l2=NOW, l3=NOW, l4=NOW),
            ["repo_publish", "busy_question", "delivery"],
        ),
        (
            LayerSuccess(
                l1=NOW,
                l2=NOW - timedelta(minutes=31),
                l3=NOW,
                l4=NOW,
            ),
            ["source_check", "repo_publish", "busy_question", "delivery"],
        ),
        (
            LayerSuccess(
                l1=NOW,
                l2=NOW,
                l3=NOW - timedelta(hours=2, seconds=1),
                l4=NOW,
            ),
            ["knowledge", "validate", "repo_publish", "busy_question", "delivery"],
        ),
        (
            LayerSuccess(
                l1=NOW,
                l2=NOW,
                l3=NOW,
                l4=datetime(2026, 8, 22, 23, tzinfo=UTC),
            ),
            ["validate", "daily", "repo_publish", "busy_question", "delivery"],
        ),
    ],
)
async def test_maintenance_tick_skips_polling_but_retains_due_work(
    tmp_path: Path,
    success: LayerSuccess,
    expected_events: list[str],
) -> None:
    pipeline = Pipeline()
    service = scheduler(tmp_path, pipeline, success)

    await service.tick(NOW, intake_policy=IntakePolicy.SKIP)

    assert pipeline.events == expected_events
    assert service.load_success().l1 == NOW


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
