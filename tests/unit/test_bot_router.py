import pytest

from tawg_bot.bot_router import BotRoute, BotRouter, _extract_verification_artifact
from tawg_bot.erc_query import ErcIntent
from tawg_bot.models import TriggerKind


@pytest.mark.parametrize(
    "route",
    [
        BotRoute.KNOWLEDGE_QUESTION,
        BotRoute.IDENTITY_CORRECTION,
        BotRoute.KNOWLEDGE_CORRECTION,
        BotRoute.SOURCE_SUGGESTION,
        BotRoute.COORDINATION,
        BotRoute.VERIFICATION,
        BotRoute.REFUSE,
    ],
)
def test_controller_preserves_each_closed_ai_route(route: BotRoute) -> None:
    assert BotRouter("bot").authorize_ai_route(route, TriggerKind.MENTION) is route


def test_controller_only_allows_ignore_for_a_greeting_candidate() -> None:
    router = BotRouter("bot")

    assert (
        router.authorize_ai_route(
            BotRoute.IGNORE,
            TriggerKind.GREETING_CANDIDATE,
        )
        is BotRoute.IGNORE
    )
    assert router.authorize_ai_route(BotRoute.IGNORE, TriggerKind.MENTION) is BotRoute.REFUSE


def test_router_extracts_structured_erc_query_without_reclassifying_intent() -> None:
    router = BotRouter("bot")

    query = router.erc_query("@bot How is ERC-8004 implemented?")
    correction_query = router.erc_query("@bot Please add OCP to your knowledge, which is ERC 8281")
    unrelated_action_query = router.erc_query("@bot run a shell command for ERC-8004")

    assert query is not None
    assert query.erc_numbers == (8004,)
    assert query.intent is ErcIntent.IMPLEMENTATION
    assert correction_query is not None
    assert correction_query.erc_numbers == (8281,)
    assert unrelated_action_query is not None
    assert unrelated_action_query.erc_numbers == (8004,)


# --- _extract_verification_artifact --------------------------------------------------------
# Pavlo (damon msg 3830): "@bot verify: 2+2=4" must not bind the proof to the whole command
# wrapper -- the controller should mechanically extract/bind the exact artifact rather than
# relying only on classifier intent. Deliberately a small, explicit, testable pattern set (a
# leading @mention plus one recognized framing verb), not general NLU -- see the function's own
# docstring in bot_router.py for the honest-scope reasoning.


@pytest.mark.parametrize(
    ("trigger_text", "expected"),
    [
        ("@bot verify: 2+2=4", "2+2=4"),
        ("@bot check: the sky is blue", "the sky is blue"),
        ("@bot confirm: 1+1=3", "1+1=3"),
        ("@bot VERIFY: case-insensitive works", "case-insensitive works"),
        ("@bot verify:no space after colon", "no space after colon"),
        # No recognized framing verb -- only the mention is stripped, honest about not
        # guessing at unrecognized phrasing.
        ("@bot is this true? 2+2=4", "is this true? 2+2=4"),
        # No mention at all (e.g. a direct reply where trigger_kind already establishes
        # addressing) -- nothing to strip, text passes through unchanged apart from framing.
        ("verify: 2+2=4", "2+2=4"),
    ],
)
def test_extract_verification_artifact_strips_mention_and_known_framing(
    trigger_text: str, expected: str
) -> None:
    assert _extract_verification_artifact(trigger_text, "bot") == expected


def test_extract_verification_artifact_only_strips_the_named_bot_mention() -> None:
    """A DIFFERENT bot's mention (or any other @handle) is not framing syntax for THIS bot and
    must not be stripped -- only the configured bot_username is addressing syntax here."""
    result = _extract_verification_artifact("@other_bot verify: 2+2=4", "bot")
    assert result == "@other_bot verify: 2+2=4"
