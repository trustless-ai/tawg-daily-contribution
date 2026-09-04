"""Durable UTC scheduling for progressively heavier L1-L4 work."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from tawg_bot.daily import DailyWindow
from tawg_bot.models import LayerSuccess
from tawg_bot.repository_session import RepositoryConflict
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class Layer(IntEnum):
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


class IntakePolicy(StrEnum):
    POLL = "poll"
    SKIP = "skip"


class LayerPipeline(Protocol):
    async def telegram_intake(self, now: datetime) -> None: ...

    async def source_check(self, now: datetime) -> None: ...

    async def knowledge_refresh(self, cutoff: datetime) -> None: ...

    async def validate(self) -> None: ...

    async def daily_prepare(self, window_id: str) -> None: ...

    async def publish_repository(self) -> None: ...

    async def telegram_delivery(self) -> None: ...

    async def maybe_ask_busy_question(self, now: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    layer: Layer
    window_id: str | None
    delivered: bool
    failed_phases: tuple[str, ...] = ()


class Scheduler:
    _STATE_PATH = "data/state/layer-success.json"

    def __init__(self, root: Path, *, pipeline: LayerPipeline) -> None:
        self.root = root.resolve()
        self.pipeline = pipeline

    def due_layer(self, now: datetime) -> Layer:
        self._require_utc(now)
        success = self.load_success()
        latest_daily = DailyWindow.for_due_run(now).end
        if success.l4 is None or success.l4 < latest_daily:
            return Layer.L4
        if success.l2 is None or now - success.l2 >= timedelta(minutes=30):
            return Layer.L2
        if success.l3 is None or now - success.l3 >= timedelta(hours=2):
            return Layer.L3
        return Layer.L1

    async def tick(
        self,
        now: datetime,
        *,
        observe_only: bool = False,
        intake_policy: IntakePolicy = IntakePolicy.POLL,
    ) -> TickResult:
        self._require_utc(now)
        layer = self.due_layer(now)
        window = DailyWindow.for_due_run(now) if layer is Layer.L4 else None
        failed_phases: list[str] = []

        if intake_policy is IntakePolicy.POLL:
            telegram_ok = await self._phase(
                "telegram_intake", self.pipeline.telegram_intake(now), failed_phases
            )
        elif intake_policy is IntakePolicy.SKIP:
            telegram_ok = True
        else:
            raise ValueError("unsupported scheduler intake policy")
        phase_ok = True
        validation_ok = True
        if layer is Layer.L2:
            phase_ok = await self._phase(
                "source_check", self.pipeline.source_check(now), failed_phases
            )
        elif layer is Layer.L3:
            phase_ok = await self._phase(
                "knowledge_refresh", self.pipeline.knowledge_refresh(now), failed_phases
            )
            validation_ok = await self._phase(
                "validate", self.pipeline.validate(), failed_phases
            )
        elif layer is Layer.L4:
            validation_ok = await self._phase(
                "validate", self.pipeline.validate(), failed_phases
            )
        daily_ok = True
        if layer is Layer.L4:
            assert window is not None
            if validation_ok:
                daily_ok = await self._phase(
                    "daily_prepare",
                    self.pipeline.daily_prepare(window.window_id),
                    failed_phases,
                )
            else:
                daily_ok = False
        repository_ok = await self._phase(
            "publish_repository", self.pipeline.publish_repository(), failed_phases
        )
        if not observe_only:
            await self._phase(
                "busy_question",
                self.pipeline.maybe_ask_busy_question(now),
                failed_phases,
            )
        delivery_ok = True
        if not observe_only:
            delivery_ok = await self._phase(
                "telegram_delivery", self.pipeline.telegram_delivery(), failed_phases
            )

        completed_layer: Layer | None = None
        base_ok = telegram_ok and repository_ok
        if base_ok:
            completed_layer = Layer.L1
            if layer is Layer.L2 and phase_ok:
                completed_layer = Layer.L2
            elif layer is Layer.L3 and phase_ok and validation_ok:
                completed_layer = Layer.L3
            elif (
                layer is Layer.L4
                and validation_ok
                and daily_ok
                and not observe_only
                and delivery_ok
            ):
                completed_layer = Layer.L4
        if completed_layer is not None:
            self._record_success(completed_layer, now)
        delivered = layer is Layer.L4 and daily_ok and not observe_only and delivery_ok
        return TickResult(
            layer=layer,
            window_id=window.window_id if window is not None else None,
            delivered=delivered,
            failed_phases=tuple(failed_phases),
        )

    def load_success(self) -> LayerSuccess:
        path = self.root / self._STATE_PATH
        if not path.exists():
            return LayerSuccess()
        try:
            return LayerSuccess.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as error:
            raise ValueError("invalid layer success state") from error

    def _record_success(self, layer: Layer, now: datetime) -> None:
        success = self.load_success()
        updates = {"l1": now}
        if layer is not Layer.L1:
            updates[layer.name.casefold()] = now
        updated = success.model_copy(update=updates)
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"layer-success:{layer.name.lower()}:{int(now.timestamp())}"
        )
        uow.register_external_evidence(())
        uow.stage_json(self._STATE_PATH, updated.model_dump(mode="json"))
        uow.publish()

    @staticmethod
    async def _phase(
        name: str, operation: Awaitable[None], failed_phases: list[str]
    ) -> bool:
        try:
            await operation
        except RepositoryConflict:
            raise
        except Exception:
            failed_phases.append(name)
            print(
                f"tawg_event=phase_failed phase={name} code={name}_failed",
                flush=True,
            )
            return False
        return True

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("scheduler time must use UTC")
