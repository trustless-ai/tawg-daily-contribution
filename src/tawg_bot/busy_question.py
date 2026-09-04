"""Proactive "the group got busy" question trigger.

When the committed Telegram history accumulates more than a configured number of messages within
a rolling window, and the cooldown has elapsed, the bot asks another bot a short, light-hearted
"what just happened" question. The threshold, window, cooldown, and target are all environment
configurable so the cadence can be tuned without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tawg_bot.models import StrictModel
from tawg_bot.query import TelegramQuery

if TYPE_CHECKING:
    from tawg_bot.unit_of_work import RepositoryUnitOfWork


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("busy-question time must use UTC")
    return value


@dataclass(frozen=True, slots=True)
class BusyQuestionConfig:
    window_seconds: int
    threshold: int
    cooldown_seconds: int
    target: str

    @classmethod
    def from_env(cls) -> BusyQuestionConfig:
        return cls(
            window_seconds=int(
                os.environ.get("TAWG_BUSY_QUESTION_WINDOW_SECONDS", "7200")
            ),
            threshold=int(os.environ.get("TAWG_BUSY_QUESTION_THRESHOLD", "14")),
            cooldown_seconds=int(
                os.environ.get("TAWG_BUSY_QUESTION_COOLDOWN_SECONDS", "7200")
            ),
            target=os.environ.get("TAWG_BUSY_QUESTION_TARGET", "@Tmerlini_bot"),
        )


class BusyQuestionState(StrictModel):
    schema_version: str = "tawg.busy-question-state.v1"
    last_triggered_at: datetime | None = None


class AskQuestionResult(StrictModel):
    schema_version: Literal["tawg.ask_question-result.v1"]
    question_text: str


@dataclass(frozen=True, slots=True)
class BusyQuestionDecision:
    should_ask: bool
    recent_count: int


class BusyQuestionService:
    _STATE_PATH = "data/state/busy-question-state.json"

    def __init__(self, root: Path, *, config: BusyQuestionConfig) -> None:
        self.root = root.resolve()
        self.config = config

    def count_recent_messages(self, now: datetime) -> int:
        _require_utc(now)
        start = now - timedelta(seconds=self.config.window_seconds)
        return sum(
            1
            for record in TelegramQuery(self.root).records()
            if start <= record.created_at <= now
        )

    def load_state(self) -> BusyQuestionState:
        path = self.root / self._STATE_PATH
        if not path.exists():
            return BusyQuestionState()
        try:
            return BusyQuestionState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("invalid busy-question state") from error

    def decide(self, now: datetime) -> BusyQuestionDecision:
        _require_utc(now)
        recent_count = self.count_recent_messages(now)
        state = self.load_state()
        cooldown_elapsed = (
            state.last_triggered_at is None
            or now - state.last_triggered_at
            >= timedelta(seconds=self.config.cooldown_seconds)
        )
        return BusyQuestionDecision(
            should_ask=recent_count >= self.config.threshold and cooldown_elapsed,
            recent_count=recent_count,
        )

    def stage_triggered(self, uow: RepositoryUnitOfWork, now: datetime) -> None:
        _require_utc(now)
        uow.stage_json(
            self._STATE_PATH,
            BusyQuestionState(last_triggered_at=now).model_dump(mode="json"),
        )
