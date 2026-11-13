from __future__ import annotations

import pytest

from tawg_bot.daily import (
    DailyCitationDiagnostics,
    DailyRejected,
    _citation_rejection,
    canonicalize_citation,
)
from tawg_bot.runtime import _daily_rejection_code


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" https://GitHub.com/trustless-ai/x/ ", "https://github.com/trustless-ai/x"),
        ("<https://WWW.GitHub.com/trustless-ai/x/>", "https://github.com/trustless-ai/x"),
        ('"https://github.com/a/b/",', "https://github.com/a/b"),
        ("HTTPS://Example.COM/A/B/", "https://example.com/A/B"),
        ("https://github.com/a/b?tab=readme#top", "https://github.com/a/b?tab=readme#top"),
        ("https://github.com:443/a/b", "https://github.com:443/a/b"),
        ("[tg:tawg:1]", "tg:tawg:1"),
        ("tg:tawg:1", "tg:tawg:1"),
    ],
)
def test_canonicalize_citation_normalizes_only_structural_noise(
    value: str, expected: str
) -> None:
    assert canonicalize_citation(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://GitHub.com/trustless-ai/x/",
        "[tg:tawg:1]",
        "https://github.com/a/b?tab=readme#top",
        "https://user:secret@github.com/x",
        "tg:Tawg:1",
    ],
)
def test_canonicalize_citation_is_idempotent(value: str) -> None:
    once = canonicalize_citation(value)
    assert canonicalize_citation(once) == once


def test_canonicalize_citation_never_rewrites_credentials_or_unknown_schemes() -> None:
    assert (
        canonicalize_citation("https://user:secret@GitHub.com/x/")
        == "https://user:secret@GitHub.com/x"
    )
    assert canonicalize_citation("tg:Tawg:1") == "tg:Tawg:1"


def test_citation_rejection_names_offenders_caps_them_and_states_the_rule() -> None:
    allowed = {"tg:tawg:1", "https://github.com/trustless-ai/agent-ercs/pull/7"}
    unknown = {"fake:4", "fake:3", "fake:2", "fake:1"}

    error = _citation_rejection("Daily citation references unknown evidence", unknown, allowed)

    message = str(error)
    assert error.code == "daily_citation_unknown"
    assert "`fake:1`, `fake:2`, `fake:3` (+1 more)" in message
    assert "`fake:4`" not in message
    assert "copied verbatim from the `citation` field" in message
    assert "not citable" in message
    assert "Nearest allowlisted citations:" in message
    assert error.citation_diagnostics == DailyCitationDiagnostics(
        unknown_count=4,
        hosts=(),
    )


def test_citation_rejection_diagnostics_keep_hostnames_only() -> None:
    unknown = {
        "https://GitHub.com/trustless-ai/private/path?token=abc#fragment",
        "tg:tawg:99",
    }

    error = _citation_rejection("Daily citation references unknown evidence", unknown, set())

    assert error.citation_diagnostics == DailyCitationDiagnostics(
        unknown_count=2,
        hosts=("github.com",),
    )


def test_daily_rejection_code_prefers_the_stable_code_attribute() -> None:
    error = DailyRejected("Model-facing citation feedback.", code="daily_citation_unknown")

    assert _daily_rejection_code(error) == "daily_citation_unknown"


@pytest.mark.parametrize(
    "message",
    [
        "Daily citation references unknown evidence",
        "Daily citation references unknown evidence: 1 draft citation(s) are not allowlisted",
        "Daily text contains an unknown citation",
        "Daily text contains an unknown citation: 1 draft citation(s) are not allowlisted",
    ],
)
def test_daily_rejection_code_still_maps_unknown_citation_messages(message: str) -> None:
    assert _daily_rejection_code(DailyRejected(message)) == "daily_citation_unknown"


def test_daily_rejection_code_falls_back_to_generic_validation() -> None:
    assert _daily_rejection_code(DailyRejected("unmapped model-facing text")) == (
        "daily_validation_failed"
    )
