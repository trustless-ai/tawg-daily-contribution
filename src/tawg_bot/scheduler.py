"""Durable UTC scheduling for progressively heavier L1-L4 work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from tawg_bot.daily import DailyWindow
from tawg_bot.models import LayerSuccess
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class Layer(IntEnum):
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


class LayerPipeline(Protocol):
    async def telegram_intake(self, now: datetime) -> None: ...

    async def source_check(self, now: datetime) -> None: ...

    async def knowledge_refresh(self, cutoff: datetime) -> None: ...

    async def validate(self) -> None: ...

    async def daily_prepare(self, window_id: str) -> None: ...

    async def publish_repository(self) -> None: ...

    async def telegram_delivery(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    layer: Layer
    window_id: str | None
    delivered: bool


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
        if success.l3 is None or now - success.l3 >= timedelta(hours=2):
            return Layer.L3
        if success.l2 is None or now - success.l2 >= timedelta(minutes=30):
            return Layer.L2
        return Layer.L1

    async def tick(self, now: datetime, *, observe_only: bool = False) -> TickResult:
        self._require_utc(now)
        layer = self.due_layer(now)
        window = DailyWindow.for_due_run(now) if layer is Layer.L4 else None

        await self.pipeline.telegram_intake(now)
        if layer >= Layer.L2:
            await self.pipeline.source_check(now)
        if layer >= Layer.L3:
            await self.pipeline.knowledge_refresh(now)
            await self.pipeline.validate()
        if layer is Layer.L4:
            assert window is not None
            await self.pipeline.daily_prepare(window.window_id)
        await self.pipeline.publish_repository()
        delivered = False
        if not observe_only:
            await self.pipeline.telegram_delivery()
            delivered = layer is Layer.L4

        completed_layer = Layer.L3 if layer is Layer.L4 and observe_only else layer
        self._record_success(completed_layer, now)
        return TickResult(
            layer=layer,
            window_id=window.window_id if window is not None else None,
            delivered=delivered,
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
        updates = {
            name: now
            for value, name in (
                (Layer.L1, "l1"),
                (Layer.L2, "l2"),
                (Layer.L3, "l3"),
                (Layer.L4, "l4"),
            )
            if value <= layer
        }
        updated = success.model_copy(update=updates)
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"layer-success:{layer.name.lower()}:{int(now.timestamp())}"
        )
        uow.register_external_evidence(())
        uow.stage_json(self._STATE_PATH, updated.model_dump(mode="json"))
        uow.publish()

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("scheduler time must use UTC")
