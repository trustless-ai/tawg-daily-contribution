"""Persist a bounded number of model requests for each Daily window."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from tawg_bot.daily import DailyAi
from tawg_bot.delivery import DeliveryCheckpoint
from tawg_bot.unit_of_work import RepositoryUnitOfWork

_STATE_PATH = "data/state/daily-attempt-limit.json"
_MAX_ATTEMPTS = 3
_SCHEMA = "tawg.daily-attempt-limit.v1"


class DailyAttemptsExhausted(RuntimeError):
    """The current window's model request limit has been reached."""


class DailyAttemptLimit:
    def __init__(
        self, root: Path, *, window_id: str, checkpoint: DeliveryCheckpoint, ai: DailyAi
    ) -> None:
        self.root = root
        self.window_id = window_id
        self.checkpoint = checkpoint
        self.ai = ai

    @staticmethod
    def _attempts(root: Path, window_id: str) -> int:
        path = root / _STATE_PATH
        if not path.exists():
            return 0
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict)
                or set(state) != {"schema_version", "window_id", "attempts"}
                or state["schema_version"] != _SCHEMA
                or not isinstance(state["window_id"], str)
                or type(state["attempts"]) is not int
                or state["attempts"] < 0
            ):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid Daily attempt state") from None
        return state["attempts"] if state["window_id"] == window_id else 0

    @classmethod
    def exhausted(cls, root: Path, window_id: str) -> bool:
        return cls._attempts(root, window_id) >= _MAX_ATTEMPTS

    async def run(
        self,
        *,
        job_type: Literal["daily"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        attempts = self._attempts(self.root, self.window_id)
        if attempts >= _MAX_ATTEMPTS:
            raise DailyAttemptsExhausted("Daily model attempt limit reached")
        count = attempts + 1
        checkpoint_id = f"daily-model-attempt:{self.window_id.replace(':', '-')}:{count}"
        uow = RepositoryUnitOfWork(self.root, operation_id=checkpoint_id)
        uow.register_external_evidence(())
        uow.stage_json(
            _STATE_PATH, {"schema_version": _SCHEMA, "window_id": self.window_id, "attempts": count}
        )
        uow.publish()
        await self.checkpoint.publish(checkpoint_id, self.root)
        return await self.ai.run(
            job_type=job_type,
            context_pack=context_pack,
            operation_id=operation_id,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
        )
