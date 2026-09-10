"""A greeting candidate is routed with the same recent context as every other trigger.

The router briefly carried the trigger alone for greeting candidates, to work around routed
jobs that kept failing. That shortcut was reverted once the real cause turned out to be an
upstream model outage -- same-day, same-size contexts routed fine before and after it. This
guards the restored behaviour so the shortcut does not come back.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tawg_bot.bot_router import BotReplyService
from tawg_bot.models import SourceRecord, TriggerKind
from tawg_bot.storage import JsonlCollection
from tests.integration.test_bot_replies import (
    NOW,
    ContextualFakeAi,
    _record,
    coordination_result,
    seed,
)


def _seed_unrelated_thread_history(root: Path) -> None:
    telegram_path = root / "data/telegram/2026/08/messages.jsonl"
    unrelated_earlier = _record(
        "tg:tawg:9",
        "An unrelated earlier message in the same thread.",
        NOW - timedelta(minutes=15),
    )
    telegram_path.write_bytes(
        JsonlCollection(telegram_path, SourceRecord).merged_bytes([unrelated_earlier])
    )


def _route_prior_ids(ai: ContextualFakeAi) -> list[str]:
    route_context = json.loads(ai.calls[0]["context_pack"])
    return [record["record_id"] for record in route_context["prior_messages"]]


@pytest.mark.asyncio
async def test_greeting_candidate_route_context_keeps_thread_history(tmp_path: Path) -> None:
    job = seed(
        tmp_path,
        "Good morning \u2600\ufe0f \nLooks clean to me.",
        trigger_kind=TriggerKind.GREETING_CANDIDATE,
    )
    _seed_unrelated_thread_history(tmp_path)
    ai = ContextualFakeAi("ignore", coordination_result())

    prepared = await BotReplyService(tmp_path, ai=ai, bot_username="bot").prepare(
        job.job_id, now=NOW + timedelta(minutes=2)
    )

    assert prepared is None
    assert "tg:tawg:9" in _route_prior_ids(ai)
