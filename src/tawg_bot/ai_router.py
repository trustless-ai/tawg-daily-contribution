"""Strict no-tools AI classification for one contextual Telegram mention."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from tawg_bot.conversation_context import ConversationContext
from tawg_bot.models import BotRoute, RouteContextScope, StrictModel


class AiRouteRejected(ValueError):
    """Raised when an AI route decision cannot cross the controller boundary."""


class RouteAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["route"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class _RouteResult(StrictModel):
    schema_version: Literal["tawg.route-result.v2"]
    route: BotRoute
    context_scope: RouteContextScope
    artifact: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: BotRoute
    context_scope: RouteContextScope
    context_sha256: str
    artifact: str | None


class ContextualAiRouter:
    """Ask one isolated AI call for exactly one allowlisted route."""

    def __init__(self, ai: RouteAi) -> None:
        self.ai = ai

    async def classify(
        self,
        context: ConversationContext,
        *,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> RouteDecision:
        raw = await self.ai.run(
            job_type="route",
            context_pack=context.text,
            operation_id=operation_id,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
        )
        try:
            result = _RouteResult.model_validate(raw)
        except ValidationError:
            raise AiRouteRejected("invalid AI route output") from None
        if result.context_scope is RouteContextScope.ERC and result.route not in {
            BotRoute.KNOWLEDGE_QUESTION,
            BotRoute.KNOWLEDGE_CORRECTION,
        }:
            raise AiRouteRejected("invalid AI route output")
        if result.route is BotRoute.VERIFICATION and not (result.artifact or "").strip():
            raise AiRouteRejected("verification route requires a non-empty artifact")
        return RouteDecision(
            route=result.route,
            context_scope=result.context_scope,
            context_sha256=context.sha256,
            artifact=result.artifact,
        )
