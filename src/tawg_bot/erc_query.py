"""Deterministic planning for explicit ERC knowledge questions."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, field_validator

from tawg_bot.models import StrictModel


class ErcQueryRejected(ValueError):
    """Raised when an explicit ERC query exceeds deterministic bounds."""


class ErcIntent(StrEnum):
    OVERVIEW = "overview"
    IMPLEMENTATION = "implementation"
    INTERFACES = "interfaces"
    STATE_MACHINE = "state_machine"
    SECURITY = "security"
    STATUS = "status"
    COMPARISON = "comparison"
    DISCUSSION = "discussion"


class ErcQuery(StrictModel):
    erc_numbers: tuple[int, ...] = Field(min_length=1, max_length=4)
    intent: ErcIntent

    @field_validator("erc_numbers")
    @classmethod
    def numbers_are_unique_and_bounded(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)) or any(not 1 <= value <= 99_999 for value in values):
            raise ValueError("ERC numbers must be unique and between 1 and 99999")
        return values


class ErcQueryPlanner:
    _URL = re.compile(r"https?://\S+", re.IGNORECASE)
    _MENTION = re.compile(r"@[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")
    _ERC = re.compile(r"(?<![A-Za-z0-9])(?:ERC|EIP)[-\s]?(\d{1,5})(?!\d)", re.IGNORECASE)
    _COMPARISON = re.compile(
        r"\b(compare|comparison|versus|vs\.?|difference|different)\b|比较|对比|区别",
        re.IGNORECASE,
    )
    _INTENTS: tuple[tuple[ErcIntent, re.Pattern[str]], ...] = (
        (
            ErcIntent.INTERFACES,
            re.compile(r"\b(interface|interfaces|ABI|function signatures?)\b|接口|函数签名", re.I),
        ),
        (
            ErcIntent.STATE_MACHINE,
            re.compile(
                r"\b(state machine|state transitions?|lifecycle|execution flow|FSM)\b|"
                r"状态机|状态转换|生命周期|执行流程",
                re.I,
            ),
        ),
        (
            ErcIntent.SECURITY,
            re.compile(r"\b(security|threat|attack|risk|failure mode)\b|安全|威胁|攻击|风险", re.I),
        ),
        (
            ErcIntent.IMPLEMENTATION,
            re.compile(
                r"\b(implement|implemented|implementation|code|contract|repository|repo|"
                r"tests?|examples?|samples?|foundry)\b|实现|代码|合约|仓库|测试|示例",
                re.I,
            ),
        ),
        (
            ErcIntent.STATUS,
            re.compile(
                r"\b(status|stage|draft|final|current version|progress)\b|状态|阶段|版本|进展",
                re.I,
            ),
        ),
        (
            ErcIntent.DISCUSSION,
            re.compile(
                r"\b(discussion|debate|rationale|contested|controversy)\b|讨论|争议|理由",
                re.I,
            ),
        ),
    )

    def plan(self, text: str) -> ErcQuery | None:
        sanitized = self._MENTION.sub(" ", self._URL.sub(" ", text))
        numbers: list[int] = []
        for match in self._ERC.finditer(sanitized):
            number = int(match.group(1))
            if not 1 <= number <= 99_999 or number in numbers:
                continue
            numbers.append(number)
            if len(numbers) > 4:
                raise ErcQueryRejected("an ERC query may reference at most four standards")
        if not numbers:
            return None
        if len(numbers) > 1 or self._COMPARISON.search(sanitized):
            intent = ErcIntent.COMPARISON
        else:
            intent = next(
                (candidate for candidate, pattern in self._INTENTS if pattern.search(sanitized)),
                ErcIntent.OVERVIEW,
            )
        return ErcQuery(erc_numbers=tuple(numbers), intent=intent)
