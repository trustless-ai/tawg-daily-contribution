import pytest

from tawg_bot.bot_router import BotReplyService, BotRoute, BotRouter, ReplyRejected
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


# --- _validate_verification_artifact (gate placeholder) ----------------------------------


def test_validate_verification_artifact_rejects_empty_artifact() -> None:
    with pytest.raises(ReplyRejected, match="no text to verify"):
        BotReplyService._validate_verification_artifact(None)
    with pytest.raises(ReplyRejected, match="no text to verify"):
        BotReplyService._validate_verification_artifact("   ")


def test_validate_verification_artifact_strips_surrounding_whitespace() -> None:
    assert BotReplyService._validate_verification_artifact("  2+2=4  ") == "2+2=4"
