"""A greeting candidate is classified from its own text, not from the surrounding thread.

Regression for tg:tawg:4585 (2026-09-10). The message opened with "Good morning" inside the
ERC-8309 companion thread, so the router folded 117 same-thread messages into a ~64KB route
context. The route model then failed with "Claude Code failed with exit status 1" on every
maintenance tick and the job never left "pending".

A greeting candidate carries no @mention and no reply-to-bot, so the trigger itself is enough
to classify it. Thread history is still supplied for every other trigger kind.
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
    reply_result,
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
async def test_greeting_candidate_route_context_omits_thread_history(tmp_path: Path) -> None:
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
    assert "tg:tawg:9" not in _route_prior_ids(ai)


@pytest.mark.asyncio
async def test_explicit_mention_route_context_keeps_thread_history(tmp_path: Path) -> None:
    job = seed(tmp_path, "@bot what should we check next?")
    _seed_unrelated_thread_history(tmp_path)
    ai = ContextualFakeAi("knowledge_question", reply_result(chinese=False))

    await BotReplyService(tmp_path, ai=ai, bot_username="bot").prepare(
        job.job_id, now=NOW + timedelta(minutes=2)
    )

    assert "tg:tawg:9" in _route_prior_ids(ai)
