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
        job_type: Literal["route", "route_entry"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class _RouteResult(StrictModel):
    """Legacy production contract; unchanged when entry mode is disabled."""

    schema_version: Literal["tawg.route-result.v2"]
    route: BotRoute
    context_scope: RouteContextScope
    artifact: str | None = None


class _EntryRouteResult(StrictModel):
    """Dev-only entry contract with an explicit decision and address judgment."""

    schema_version: Literal["tawg.route-entry-result.v1"]
    decision: Literal["reply", "ignore"]
    addressed_to_bot: bool
    route: BotRoute
    context_scope: RouteContextScope
    artifact: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: BotRoute
    context_scope: RouteContextScope
    context_sha256: str
    artifact: str | None
    decision: Literal["reply", "ignore"] = "reply"
    addressed_to_bot: bool = False


class ContextualAiRouter:
    """Ask one isolated AI call for exactly one allowlisted route."""

    def __init__(self, ai: RouteAi, *, entry_mode: bool = False) -> None:
        self.ai = ai
        self.entry_mode = entry_mode

    async def classify(
        self,
        context: ConversationContext,
        *,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> RouteDecision:
        raw = await self.ai.run(
            job_type="route_entry" if self.entry_mode else "route",
            context_pack=context.text,
            operation_id=operation_id,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
        )
        if self.entry_mode:
            return self._classify_entry(raw, context)
        return self._classify_legacy(raw, context)

    def _classify_legacy(
        self,
        raw: dict[str, Any],
        context: ConversationContext,
    ) -> RouteDecision:
        try:
            result = _RouteResult.model_validate(raw)
        except ValidationError:
            raise AiRouteRejected("invalid AI route output") from None
        decision: Literal["reply", "ignore"] = (
            "ignore" if result.route is BotRoute.IGNORE else "reply"
        )
        self._validate_reply_route(result.route, result.context_scope, result.artifact)
        return RouteDecision(
            route=result.route,
            context_scope=result.context_scope,
            context_sha256=context.sha256,
            artifact=result.artifact,
            decision=decision,
            addressed_to_bot=False,
        )

    def _classify_entry(
        self,
        raw: dict[str, Any],
        context: ConversationContext,
    ) -> RouteDecision:
        try:
            result = _EntryRouteResult.model_validate(raw)
        except ValidationError:
            raise AiRouteRejected("invalid AI entry output") from None
        if result.decision == "ignore":
            if result.route is not BotRoute.IGNORE:
                raise AiRouteRejected("inconsistent AI entry output")
            if result.artifact not in {None, ""}:
                raise AiRouteRejected("inconsistent AI entry output")
        else:
            if result.route is BotRoute.IGNORE:
                raise AiRouteRejected("inconsistent AI entry output")
            self._validate_reply_route(
                result.route,
                result.context_scope,
                result.artifact,
            )
        return RouteDecision(
            route=result.route,
            context_scope=result.context_scope,
            context_sha256=context.sha256,
            artifact=result.artifact,
            decision=result.decision,
            addressed_to_bot=result.addressed_to_bot,
        )

    @staticmethod
    def _validate_reply_route(
        route: BotRoute,
        context_scope: RouteContextScope,
        artifact: str | None,
    ) -> None:
        if route is BotRoute.IGNORE:
            return
        if context_scope is RouteContextScope.ERC and route not in {
            BotRoute.KNOWLEDGE_QUESTION,
            BotRoute.KNOWLEDGE_CORRECTION,
        }:
            raise AiRouteRejected("invalid AI route output")
        if route is BotRoute.VERIFICATION and not (artifact or "").strip():
            raise AiRouteRejected("verification route requires a non-empty artifact")
