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
    r"\b(score[sd]?|leaderboard|rank(?:ed|ing|s)?|first place|top contributor|"
    r"priorit(?:y|ies)|"
    r"tiers?|winners?|mvp|hero|I did|my work|"
    r"earned reward|reward eligibility|payout|on-chain credit)\b",
    re.IGNORECASE,
)
_OTHER_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_DIRECTION_LABEL = re.compile(r"^\*\*[^*\n]+\*\*$")
_CONCRETE_SYNTHESIS = re.compile(
    r"(?:\d|https?://|www\.|\b(?:merge[ds]?|open(?:ed|s)?|publish(?:ed|es)?|"
    r"ship(?:ped|s)?|implement(?:ed|s)?|clarif(?:ied|ies)|resolv(?:ed|es)|"
    r"fix(?:ed|es)?|review(?:ed|s)?|commit(?:ted|s)?|creat(?:ed|es)|"
    r"submit(?:ted|s)?|reproduc(?:ed|es)|test(?:ed|s)?|deploy(?:ed|s)?|"
    r"releas(?:ed|es))\b)",
    re.IGNORECASE,
)
_PLAIN_CITATION = re.compile(r"\[([^\[\]\n]+)\](?!\()")
_MARKDOWN_CITATION = re.compile(r"\[[^\[\]\n]+\]\(([^()\s]+)\)")
_TRAILING_CITATIONS = re.compile(r"(?:\s+\[[^\[\]\n]+\](?:\([^()\s]+\))?)+$")


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
                        "score",
                        "leaderboard",
                        "rank",
                        "ranked",
                        "ranking",
                        "first place",
                        "top contributor",
                        "priority",
                        "tier",
                        "tiers",
                        "winner",
                        "winners",
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
                        "Each direction may have one uncited synthesis sentence; every concrete "
                        "What moved bullet starts with • and ends with an exact allowlisted "
                        "citation."
                    ),
                    "ordering_rule": (
                        "Order directions and items by contribution impact and importance, "
                        "without saying that anyone is ranked or scored."
                    ),
                    "what_moved_rule": (
                        "Integrate appreciation into each concrete item: name who did what, what "
                        "it advanced, and why it helps the group or Trustless AI. Do not add a "
                        "separate Appreciation section."
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
        if any(self._heading_name(line).casefold() == "appreciation" for line in lines):
            raise DailyRejected("Daily must integrate Appreciation into What moved")
        first_line = lines[0]
        if first_line != required_title:
            emoji = _EMOJI.match(first_line)
            if emoji is None or first_line[emoji.end() :].lstrip() != required_title:
                raise DailyRejected("Daily title must match the exact UTC window")
        section_indices: list[int] = []
        for section in self.policy["required_sections"]:
            indices = [
                index for index, line in enumerate(lines) if self._section_name(line) == section
            ]
            if len(indices) != 1:
                raise DailyRejected(f"Daily output has an invalid required section: {section}")
            section_indices.append(indices[0])
        if section_indices != sorted(section_indices):
            raise DailyRejected("Daily output has required sections out of order")
        allowed_emoji_headings = {
            *self.policy["required_sections"],
            "ideas to follow",
            "todos",
        }
        for line in lines[1:]:
            if (
                _EMOJI.match(line.strip())
                and self._heading_name(line) not in allowed_emoji_headings
            ):
                raise DailyRejected("Daily output has an unexpected top-level section")
        self._validate_next_up(lines[section_indices[1] + 1 :])

        allowed_citations = {item.citation for item in evidence}
        if len(result.citations) != len(set(result.citations)):
            raise DailyRejected("Daily citation list contains duplicates")
        if not set(result.citations).issubset(allowed_citations):
            raise DailyRejected("Daily citation references unknown evidence")
        text_citations = set(_PLAIN_CITATION.findall(result.telegram_text)) | set(
            _MARKDOWN_CITATION.findall(result.telegram_text)
        )
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
            self._validate_what_moved(
                lines[section_indices[0] + 1 : section_indices[1]],
                set(result.citations),
                {item.author_person_id.casefold() for item in evidence if item.author_person_id},
            )
        return result

    @staticmethod
    def _validate_what_moved(lines: list[str], citations: set[str], contributors: set[str]) -> None:
        content = [line.strip() for line in lines if line.strip()]
        state = "label"
        bullet_seen = False
        direction_seen = False
        for line in content:
            if state == "label":
                if not _DIRECTION_LABEL.fullmatch(line):
                    raise DailyRejected("Daily What moved has an invalid direction structure")
                direction_seen = True
                state = "synthesis"
                continue
            if state == "synthesis":
                normalized_words = re.sub(r"[^\w]+", " ", line.casefold()).strip()
                normalized = f" {normalized_words} "
                contributor_words = {
                    re.sub(r"[^\w]+", " ", contributor).strip() for contributor in contributors
                }
                names_contributor = any(
                    f" {contributor} " in normalized
                    for contributor in contributor_words
                    if contributor
                )
                if (
                    _DIRECTION_LABEL.fullmatch(line)
                    or line.startswith("• ")
                    or _OTHER_LIST_ITEM.match(line)
                    or _PLAIN_CITATION.search(line)
                    or _MARKDOWN_CITATION.search(line)
                    or _CONCRETE_SYNTHESIS.search(line)
                    or names_contributor
                ):
                    raise DailyRejected("Daily synthesis contains source-dependent detail")
                state = "bullets"
                continue
            if _DIRECTION_LABEL.fullmatch(line):
                if not bullet_seen:
                    raise DailyRejected("Daily What moved has an invalid direction structure")
                bullet_seen = False
                state = "synthesis"
                continue
            if _OTHER_LIST_ITEM.match(line):
                raise DailyRejected("Daily concrete progress uses an invalid bullet marker")
            if not line.startswith("• "):
                raise DailyRejected("Daily What moved has an invalid direction structure")
            bullet_seen = True
            trailing = _TRAILING_CITATIONS.search(line)
            line_citations = (
                set(_PLAIN_CITATION.findall(trailing.group()))
                | set(_MARKDOWN_CITATION.findall(trailing.group()))
                if trailing
                else set()
            )
            all_line_citations = set(_PLAIN_CITATION.findall(line)) | set(
                _MARKDOWN_CITATION.findall(line)
            )
            if (
                not line_citations
                or not line_citations.issubset(citations)
                or not all_line_citations.issubset(citations)
            ):
                raise DailyRejected("Daily factual bullet lacks a valid citation")
        if not direction_seen or state != "bullets" or not bullet_seen:
            raise DailyRejected("active Daily lacks a complete What moved direction")

    def _validate_next_up(self, lines: list[str]) -> None:
        content = [line.strip() for line in lines if line.strip()]
        if not content or self._heading_name(content[0]) != "ideas to follow":
            raise DailyRejected("Daily output has an unexpected top-level section")
        todo_indices = [
            index for index, line in enumerate(content) if self._heading_name(line) == "todos"
        ]
        if len(todo_indices) != 1 or todo_indices[0] < 2:
            raise DailyRejected("Daily output has an unexpected top-level section")
        todo_index = todo_indices[0]
        ideas = content[1:todo_index]
        if not ideas or any(not line.startswith("• ") for line in ideas):
            raise DailyRejected("Daily output has an unexpected top-level section")
        remainder = content[todo_index + 1 :]
        todo_count = 0
        while todo_count < len(remainder) and remainder[todo_count].startswith("• "):
            todo_count += 1
        closing = remainder[todo_count:]
        if todo_count == 0 or len(closing) != 1:
            raise DailyRejected("Daily output has an unexpected top-level section")
        if (
            _DIRECTION_LABEL.fullmatch(closing[0])
            or _OTHER_LIST_ITEM.match(closing[0])
            or _EMOJI.match(closing[0])
        ):
            raise DailyRejected("Daily output has an unexpected top-level section")

    def _section_name(self, line: str) -> str | None:
        name = self._heading_name(line)
        return name if name in self.policy["required_sections"] else None

    @staticmethod
    def _heading_name(line: str) -> str:
        stripped = line.strip().strip("#").strip()
        emoji = _EMOJI.match(stripped)
        name = stripped[emoji.end() :].strip() if emoji is not None else stripped
        return name.strip("*: ")

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
