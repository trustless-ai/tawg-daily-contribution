"""Fixed-window, evidence-grounded Daily catch-up preparation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import field_validator

from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.daily_evidence import DailyEvidence
from tawg_bot.models import DeliveryAttempt, DeliveryStatus, StrictModel
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.telegram_text import TelegramTextSplitError, split_telegram_text

_NON_ENGLISH_SCRIPT = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\u0400-\u052f\u0600-\u06ff\u0900-\u097f]"
)
_EMOJI = re.compile("[\U0001f1e6-\U0001f1ff\U0001f300-\U0001faff\u2600-\u27bf]")
_DISALLOWED_TONE = re.compile(
    r"\b(rank(?:ed|ing|s)?|score[sd]?|leaderboard|mvp|hero|I did|my work|"
    r"earned reward|reward eligibility|payout|on-chain credit)\b",
    re.IGNORECASE,
)
_CITATION = re.compile(r"\[([^\[\]\n]+)\]")
_TRAILING_CITATIONS = re.compile(r"(?:\s+\[[^\[\]\n]+\])+$")


class DailyRejected(ValueError):
    """Raised when Daily preparation is stale, ungrounded, or off-policy."""


@dataclass(frozen=True, slots=True)
class DailyWindow:
    start: datetime
    end: datetime
    window_id: str

    @classmethod
    def for_due_run(cls, run_at: datetime) -> DailyWindow:
        _require_utc(run_at, "Daily run time")
        due = run_at.replace(hour=23, minute=0, second=0, microsecond=0)
        if due > run_at:
            due -= timedelta(days=1)
        return cls(
            start=due - timedelta(days=1),
            end=due,
            window_id=f"daily:{due.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        )

    def contains(self, value: datetime) -> bool:
        _require_utc(value, "Daily evidence time")
        return self.start <= value < self.end


@dataclass(frozen=True, slots=True)
class DailyReadiness:
    telegram_synced_at: datetime
    live_evidence_collected_at: datetime
    knowledge_refreshed_at: datetime

    def assert_fresh_for(self, window: DailyWindow) -> None:
        for name, value in (
            ("Telegram", self.telegram_synced_at),
            ("live evidence", self.live_evidence_collected_at),
            ("knowledge", self.knowledge_refreshed_at),
        ):
            _require_utc(value, f"{name} readiness")
            if value < window.end:
                raise DailyRejected(f"{name} is not fresh through the Daily cutoff")


class DailyAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["daily"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class _DailyResult(StrictModel):
    schema_version: Literal["tawg.daily-result.v1"]
    window_id: str
    window_start: datetime
    window_end: datetime
    telegram_text: str
    citations: list[str]
    quiet_day: bool

    @field_validator("window_start", "window_end")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "Daily result time")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PreparedDaily:
    window_id: str
    telegram_text: str
    messages: tuple[str, ...]
    citations: tuple[str, ...]
    quiet_day: bool


class DailyService:
    def __init__(
        self,
        root: Path,
        *,
        ai: DailyAi,
        timeout_seconds: float = 600,
    ) -> None:
        self.root = root.resolve()
        self.ai = ai
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")
        self.policy = self._load_policy()

    async def prepare(
        self,
        window: DailyWindow,
        *,
        readiness: DailyReadiness,
        evidence: tuple[DailyEvidence, ...],
    ) -> PreparedDaily | None:
        if self._already_delivered(window.window_id):
            return None
        readiness.assert_fresh_for(window)
        if any(not window.contains(item.updated_at) for item in evidence):
            raise DailyRejected("Daily evidence falls outside the fixed UTC window")
        context = self._context(window, evidence)
        operation_id = window.window_id.replace(":", "-")
        raw = await self.ai.run(
            job_type="daily",
            context_pack=context,
            operation_id=operation_id,
            max_budget_usd=str(self.policy["max_model_budget_usd"]),
            timeout_seconds=self.timeout_seconds,
        )
        result = self._validate_result(raw, window, evidence)
        messages = self._split(result.telegram_text)
        return PreparedDaily(
            window_id=result.window_id,
            telegram_text=result.telegram_text,
            messages=messages,
            citations=tuple(result.citations),
            quiet_day=result.quiet_day,
        )

    def _context(self, window: DailyWindow, evidence: tuple[DailyEvidence, ...]) -> str:
        evidence_payload = []
        for item in evidence:
            payload = asdict(item)
            payload["created_at"] = item.created_at.isoformat()
            payload["updated_at"] = item.updated_at.isoformat()
            evidence_payload.append(payload)
        query = " ".join(item.text for item in evidence)[:8000]
        if not query:
            query = "open threads help wanted Trustless AI"
        current_context = [
            {
                "chunk_id": item.chunk_id,
                "path": item.path,
                "text": item.text,
                "record_id": item.record_id,
                "source_locator": item.source_locator,
            }
            for item in VaultRetriever(self.root).query(query, top_k=12)
            if item.path.startswith("knowledge/")
        ]
        schema = self._json_mapping(self.root / "src/tawg_bot/schemas/daily-result.v1.json")
        inputs = ContextInputs(
            trigger={
                "operation": "prepare_daily_catch_up",
                "window_id": window.window_id,
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "required_title": (
                    "TAWG Daily Catch-up — "
                    f"{window.start.strftime('%Y-%m-%d %H:%M')} UTC → "
                    f"{window.end.strftime('%Y-%m-%d %H:%M')} UTC"
                ),
                "output_contract": {
                    "required_sections": list(self.policy["required_sections"]),
                    "forbidden_terms": [
                        "rank",
                        "ranking",
                        "score",
                        "leaderboard",
                        "MVP",
                        "hero",
                        "I did",
                        "my work",
                        "earned reward",
                        "reward eligibility",
                        "payout",
                        "on-chain credit",
                    ],
                    "max_emoji": self.policy["max_emoji"],
                    "citation_rule": (
                        "Every factual bullet except Next step ends with [citation]."
                    ),
                },
                "window_evidence": evidence_payload,
                "quiet_day_required": not evidence,
            },
            reply_chain=[],
            recent_telegram=[
                item for item in evidence_payload if item["source_kind"] == "telegram"
            ],
            retrieved=current_context,
            citations=[
                {
                    "evidence_id": item.evidence_id,
                    "source_kind": item.source_kind,
                    "source_url": item.source_url,
                    "citation": item.citation,
                }
                for item in evidence
            ],
            aliases=self._yaml_mapping(self.root / "knowledge/meta/aliases.yml"),
            job_state={"window_id": window.window_id, "status": "preparing"},
            allowed_paths=[],
            output_schema=schema,
            budgets={
                "max_output_chars": 8000,
                "max_messages": self.policy["max_messages"],
                "max_emoji": self.policy["max_emoji"],
            },
            citation_allowlist=[item.citation for item in evidence],
        )
        try:
            return (
                ContextPackBuilder(self.privacy)
                .build(
                    inputs,
                    max_chars=int(self.policy["max_context_chars"]),
                    max_recent_telegram=len(evidence),
                )
                .text
            )
        except ContextRejected as error:
            raise DailyRejected(str(error)) from None

    def _validate_result(
        self,
        raw: Mapping[str, Any],
        window: DailyWindow,
        evidence: tuple[DailyEvidence, ...],
    ) -> _DailyResult:
        try:
            result = _DailyResult.model_validate(raw)
        except ValueError as error:
            raise DailyRejected("invalid Daily model output") from error
        if (
            result.window_id != window.window_id
            or result.window_start != window.start
            or result.window_end != window.end
        ):
            raise DailyRejected("Daily output changed the fixed UTC window")
        if len(result.telegram_text) > 8000:
            raise DailyRejected("Daily output exceeds the Telegram budget")
        try:
            self.privacy.assert_public(result.telegram_text)
        except PrivacyViolation:
            raise DailyRejected("Daily output failed privacy validation") from None
        if _NON_ENGLISH_SCRIPT.search(result.telegram_text):
            raise DailyRejected("Daily output must be English")
        if len(_EMOJI.findall(result.telegram_text)) > int(self.policy["max_emoji"]):
            raise DailyRejected("Daily output exceeds the emoji limit")
        tone_violation = _DISALLOWED_TONE.search(result.telegram_text)
        if tone_violation:
            raise DailyRejected("Daily output contains ranking or persona language")
        required_title = (
            "TAWG Daily Catch-up — "
            f"{window.start.strftime('%Y-%m-%d %H:%M')} UTC → "
            f"{window.end.strftime('%Y-%m-%d %H:%M')} UTC"
        )
        lines = result.telegram_text.splitlines()
        first_line = lines[0]
        if first_line != required_title:
            emoji = _EMOJI.match(first_line)
            if emoji is None or first_line[emoji.end() :].lstrip() != required_title:
                raise DailyRejected("Daily title must match the exact UTC window")
        section_indices: list[int] = []
        for section in self.policy["required_sections"]:
            indices = [
                index
                for index, line in enumerate(lines)
                if self._section_name(line) == section
            ]
            if len(indices) != 1:
                raise DailyRejected(f"Daily output has an invalid required section: {section}")
            section_indices.append(indices[0])
        if section_indices != sorted(section_indices):
            raise DailyRejected("Daily output has required sections out of order")

        allowed_citations = {item.citation for item in evidence}
        if len(result.citations) != len(set(result.citations)):
            raise DailyRejected("Daily citation list contains duplicates")
        if not set(result.citations).issubset(allowed_citations):
            raise DailyRejected("Daily citation references unknown evidence")
        text_citations = set(_CITATION.findall(result.telegram_text))
        if not text_citations.issubset(set(result.citations)):
            raise DailyRejected("Daily text contains an unknown citation")
        if evidence and (
            result.quiet_day or not set(result.citations).intersection(allowed_citations)
        ):
            raise DailyRejected("active Daily lacks a citation from its fixed window")
        if not evidence and not result.quiet_day:
            raise DailyRejected("quiet Daily invents source-backed progress")
        if result.quiet_day and "No source-backed progress landed" not in result.telegram_text:
            raise DailyRejected("quiet Daily must state that no source-backed progress landed")
        if not result.quiet_day:
            current_section = ""
            for line in lines:
                section = self._section_name(line)
                if section is not None:
                    current_section = section
                    continue
                if not line.lstrip().startswith("- "):
                    continue
                if current_section == "Next step":
                    continue
                trailing = _TRAILING_CITATIONS.search(line)
                line_citations = set(_CITATION.findall(trailing.group())) if trailing else set()
                all_line_citations = set(_CITATION.findall(line))
                if (
                    not line_citations
                    or not line_citations.issubset(result.citations)
                    or not all_line_citations.issubset(result.citations)
                ):
                    raise DailyRejected("Daily factual bullet lacks a valid citation")
        return result

    def _section_name(self, line: str) -> str | None:
        stripped = line.strip()
        emoji = _EMOJI.match(stripped)
        name = stripped[emoji.end() :].strip() if emoji is not None else stripped
        return name if name in self.policy["required_sections"] else None

    def _split(self, text: str) -> tuple[str, ...]:
        try:
            return split_telegram_text(
                text,
                limit=int(self.policy["telegram_message_chars"]),
                max_messages=int(self.policy["max_messages"]),
            )
        except TelegramTextSplitError:
            raise DailyRejected("Daily cannot fit in at most two Telegram messages") from None
        except ValueError as error:
            raise DailyRejected("invalid Daily Telegram split policy") from error

    def _already_delivered(self, window_id: str) -> bool:
        path = self.root / "data/state/delivery-state.json"
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            attempts = [DeliveryAttempt.model_validate(item) for item in raw]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise DailyRejected("invalid delivery state") from error
        return any(
            attempt.job_id == window_id and attempt.status is DeliveryStatus.DELIVERED
            for attempt in attempts
        )

    def _load_policy(self) -> dict[str, Any]:
        raw = self._yaml_mapping(self.root / "config/bot-policy.yml")
        daily = raw.get("daily")
        if raw.get("schema") != "tawg.bot-policy.v1" or not isinstance(daily, dict):
            raise DailyRejected("invalid bot policy")
        required = {
            "telegram_message_chars",
            "max_messages",
            "max_emoji",
            "max_model_budget_usd",
            "max_context_chars",
            "required_sections",
        }
        if not required.issubset(daily):
            raise DailyRejected("incomplete Daily bot policy")
        return daily

    @staticmethod
    def _json_mapping(path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise DailyRejected("Daily schema must be an object")
        return raw

    @staticmethod
    def _yaml_mapping(path: Path) -> dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise DailyRejected("configuration must be a mapping")
        return raw


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
