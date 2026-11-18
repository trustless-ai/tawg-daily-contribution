from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.daily import DailyRejected, DailyService, DailyWindow
from tawg_bot.daily_evidence import DailyEvidence
from tawg_bot.runtime import _daily_rejection_code

PROJECT = Path(__file__).parents[2]
WINDOW = DailyWindow.for_due_run(datetime(2026, 8, 24, 1, 17, tzinfo=UTC))


class _UnusedAi:
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"validation tests must not call the model: {kwargs}")


def _service(root: Path) -> DailyService:
    for relative in ("config/privacy.yml", "config/bot-policy.yml"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    aliases = root / "knowledge/meta/aliases.yml"
    aliases.parent.mkdir(parents=True)
    aliases.write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    return DailyService(root, ai=_UnusedAi())


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((PROJECT / f"tests/fixtures/ai/{name}.json").read_text())


def _active_evidence() -> tuple[DailyEvidence, ...]:
    return (
        DailyEvidence(
            evidence_id="tg:tawg:1",
            source_kind="telegram",
            source_url="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:1",
            created_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
            updated_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
            author_person_id="alice",
            text="Alice clarified ERC-8004 validation behavior.",
        ),
    )


def test_mention_outside_what_moved_names_the_offending_handle(tmp_path: Path) -> None:
    raw = _fixture("daily-active")
    raw["telegram_text"] = raw["telegram_text"].replace(
        "That's one less fog bank for the builders.",
        "@unexpected_handle, that's one less fog bank for the builders.",
    )

    with pytest.raises(DailyRejected) as caught:
        _service(tmp_path)._validate_result(raw, WINDOW, _active_evidence())

    assert str(caught.value) == (
        "Daily contains an invalid Telegram mention outside What moved: @unexpected_handle"
    )


def test_clear_day_mention_names_the_offending_handle(tmp_path: Path) -> None:
    raw = _fixture("daily-quiet")
    raw["telegram_text"] = raw["telegram_text"].replace(
        "No source-backed progress landed in this window.",
        "No source-backed progress landed in this window. @quiet_handle",
    )

    with pytest.raises(DailyRejected) as caught:
        _service(tmp_path)._validate_result(raw, WINDOW, ())

    assert str(caught.value) == (
        "Daily clear-day output contains an invalid Telegram mention: @quiet_handle"
    )


def test_what_moved_direction_line_names_the_offending_handle(tmp_path: Path) -> None:
    raw = _fixture("daily-active")
    raw["telegram_text"] = raw["telegram_text"].replace(
        "- Alice — **Clarified**", "- Alice (@unmapped_handle) — **Clarified**"
    )

    with pytest.raises(DailyRejected) as caught:
        _service(tmp_path)._validate_result(raw, WINDOW, _active_evidence())

    assert str(caught.value) == (
        "Daily What moved direction line contains an invalid Telegram mention: "
        "@unmapped_handle"
    )


@pytest.mark.parametrize(
    "message",
    [
        "Daily contains an invalid Telegram mention outside What moved: @unexpected_handle",
        "Daily clear-day output contains an invalid Telegram mention: @quiet_handle",
        (
            "Daily What moved direction line contains an invalid Telegram mention: "
            "@unmapped_handle"
        ),
    ],
)
def test_detailed_mention_messages_keep_the_stable_rejection_code(message: str) -> None:
    assert _daily_rejection_code(DailyRejected(message)) == "daily_mention_invalid"
