from __future__ import annotations

import pytest

from tawg_bot.daily import DailyRejected, DailyService, _disallowed_tone_terms
from tawg_bot.runtime import _daily_rejection_code, _log_safe_text


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Daily output contains ranking or persona language", "daily_tone_invalid"),
        (
            "Daily output contains ranking or persona language: priority",
            "daily_tone_invalid",
        ),
        (
            "Daily output contains ranking or persona language: score, winner",
            "daily_tone_invalid",
        ),
        ("Daily output exceeds the emoji limit", "daily_tone_invalid"),
        ("Daily output exceeds the emoji limit: 12 > 10", "daily_tone_invalid"),
        ("Daily Trusty's take must end with an emoji", "daily_tone_invalid"),
        (
            "Daily Trusty's take must end with an emoji: no emoji found",
            "daily_tone_invalid",
        ),
        (
            "Daily Trusty's take must end with an emoji: trailing '.'",
            "daily_tone_invalid",
        ),
        ("Daily must integrate Appreciation into What moved", "daily_tone_invalid"),
    ],
)
def test_daily_rejection_code_keeps_tone_messages_mapped_with_suffixes(
    message: str, expected: str
) -> None:
    assert _daily_rejection_code(DailyRejected(message)) == expected


def test_daily_rejection_code_prefers_the_longest_known_prefix() -> None:
    assert (
        _daily_rejection_code(
            DailyRejected("Daily Trusty's take contains source-dependent detail: [x]")
        )
        == "daily_grounding_invalid"
    )
    assert (
        _daily_rejection_code(
            DailyRejected("Daily Trusty's take lacks Today's spark: extra")
        )
        == "daily_structure_invalid"
    )


def test_disallowed_tone_terms_are_unique_lowercase_and_capped() -> None:
    text = "Priority score leaderboard ranked PRIORITY"

    assert _disallowed_tone_terms(text) == ["priority", "score", "leaderboard"]


def test_disallowed_tone_terms_ignores_clean_text() -> None:
    assert _disallowed_tone_terms("The group clarified validation behavior.") == []


def test_trustys_take_rejection_reports_a_missing_emoji() -> None:
    with pytest.raises(DailyRejected) as caught:
        DailyService._validate_trustys_take(
            [
                "> **Today's spark:** Something settled.",
                ">",
                "> Keep building, TAWG.",
            ]
        )

    assert str(caught.value) == (
        "Daily Trusty's take must end with an emoji: no emoji found"
    )


def test_trustys_take_rejection_reports_the_trailing_residue() -> None:
    with pytest.raises(DailyRejected) as caught:
        DailyService._validate_trustys_take(
            [
                "> **Today's spark:** Something settled.",
                ">",
                "> Keep building, TAWG. 🤖.",
            ]
        )

    assert str(caught.value) == (
        "Daily Trusty's take must end with an emoji: trailing '.'"
    )


def test_trustys_take_rejection_truncates_the_trailing_residue() -> None:
    residue = "!" + "x" * 100

    with pytest.raises(DailyRejected) as caught:
        DailyService._validate_trustys_take(
            [
                "> **Today's spark:** Something settled.",
                ">",
                f"> Keep building, TAWG. 🤖{residue}",
            ]
        )

    prefix = "Daily Trusty's take must end with an emoji: trailing "
    message = str(caught.value)
    assert message.startswith(prefix)
    assert message.removeprefix(prefix) == repr(residue[:60])


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                "",
                "> **Today's spark:** Something settled.",
                "",
                "> Keep building, TAWG. 🤖",
            ],
            [
                "",
                "> **Today's spark:** Something settled.",
                ">",
                "> Keep building, TAWG. 🤖",
            ],
        ),
        (
            [
                "",
                "> **Today's spark:** Something settled.   ",
                ">   ",
                "> Keep building, TAWG. 🤖   ",
            ],
            [
                "",
                "> **Today's spark:** Something settled.",
                ">",
                "> Keep building, TAWG. 🤖",
            ],
        ),
        (
            [
                "",
                "> **Today's spark:** Something settled.",
                ">",
                "> An extra take line.",
                "> Keep building, TAWG. 🤖",
            ],
            [
                "",
                "> **Today's spark:** Something settled.",
                ">",
                "> An extra take line.",
                "> Keep building, TAWG. 🤖",
            ],
        ),
    ],
)
def test_normalize_trustys_take_only_repairs_blank_lines_and_whitespace(
    lines: list[str], expected: list[str]
) -> None:
    assert DailyService._normalize_trustys_take(lines) == expected


def test_log_safe_text_collapses_whitespace() -> None:
    assert _log_safe_text("Daily failed:\n\n  reason\tone   two") == (
        "Daily failed: reason one two"
    )


def test_log_safe_text_default_limit_is_240() -> None:
    bounded = _log_safe_text("z" * 400)

    assert bounded == "z" * 240
    assert "\n" not in bounded
