from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.ai_router import AiRouteRejected, ContextualAiRouter
from tawg_bot.bot_router import BotRoute
from tawg_bot.conversation_context import ConversationContext

ROOT = Path(__file__).parents[2]


class FakeRouteAi:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.result)


def _context() -> ConversationContext:
    return ConversationContext(
        text='{"context_schema":"tawg.route-context.v1"}',
        sha256="a" * 64,
        trigger_record_id="tg:tawg:101",
        record_ids=("tg:tawg:100", "tg:tawg:101"),
        omitted_items=0,
    )


@pytest.mark.asyncio
async def test_contextual_router_returns_one_closed_route() -> None:
    ai = FakeRouteAi(
        {
            "schema_version": "tawg.route-result.v2",
            "route": "knowledge_correction",
            "context_scope": "conversation",
        }
    )

    decision = await ContextualAiRouter(ai).classify(
        _context(),
        operation_id="reply:tg:tawg:101:route",
        max_budget_usd="0.20",
        timeout_seconds=45,
    )

    assert decision.route is BotRoute.KNOWLEDGE_CORRECTION
    assert decision.context_scope.value == "conversation"
    assert decision.context_sha256 == "a" * 64
    assert ai.calls == [
        {
            "job_type": "route",
            "context_pack": '{"context_schema":"tawg.route-context.v1"}',
            "operation_id": "reply:tg:tawg:101:route",
            "max_budget_usd": "0.20",
            "timeout_seconds": 45,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {
            "schema_version": "tawg.route-result.v2",
            "route": "shell",
            "context_scope": "knowledge",
        },
        {
            "schema_version": "tawg.route-result.v2",
            "route": "coordination",
            "context_scope": "conversation",
            "reasoning": "hidden reasoning must not cross the boundary",
        },
        {
            "schema_version": "tawg.route-result.v2",
            "route": "knowledge_question",
            "context_scope": "open_web",
        },
        {
            "schema_version": "tawg.route-result.v2",
            "route": "coordination",
            "context_scope": "erc",
        },
    ],
)
async def test_contextual_router_rejects_open_or_extended_outputs(
    result: dict[str, Any],
) -> None:
    with pytest.raises(AiRouteRejected, match="invalid AI route output"):
        await ContextualAiRouter(FakeRouteAi(result)).classify(
            _context(),
            operation_id="reply:tg:tawg:101:route",
            max_budget_usd="0.20",
            timeout_seconds=45,
        )


@pytest.mark.asyncio
async def test_contextual_router_accepts_verification_with_artifact() -> None:
    ai = FakeRouteAi(
        {
            "schema_version": "tawg.route-result.v2",
            "route": "verification",
            "context_scope": "conversation",
            "artifact": "2+2=4",
        }
    )

    decision = await ContextualAiRouter(ai).classify(
        _context(),
        operation_id="reply:tg:tawg:101:route",
        max_budget_usd="0.20",
        timeout_seconds=45,
    )

    assert decision.route is BotRoute.VERIFICATION
    assert decision.artifact == "2+2=4"


@pytest.mark.asyncio
async def test_contextual_router_rejects_verification_without_artifact() -> None:
    ai = FakeRouteAi(
        {
            "schema_version": "tawg.route-result.v2",
            "route": "verification",
            "context_scope": "conversation",
        }
    )

    with pytest.raises(AiRouteRejected, match="artifact"):
        await ContextualAiRouter(ai).classify(
            _context(),
            operation_id="reply:tg:tawg:101:route",
            max_budget_usd="0.20",
            timeout_seconds=45,
        )


def test_route_result_schema_allows_verification_route() -> None:
    schema = json.loads(
        (ROOT / "src/tawg_bot/schemas/route-result.v2.json").read_text(encoding="utf-8")
    )

    assert "verification" in schema["properties"]["route"]["enum"]
    assert "artifact" in schema["properties"]
