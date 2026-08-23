from __future__ import annotations

import pytest

from tawg_bot.erc_query import ErcIntent, ErcQueryPlanner, ErcQueryRejected


@pytest.mark.parametrize(
    ("text", "numbers", "intent"),
    [
        ("How is ERC-8004 implemented?", (8004,), ErcIntent.IMPLEMENTATION),
        ("Which interfaces does ERC 8183 define?", (8183,), ErcIntent.INTERFACES),
        ("Explain the ERC-8183 state machine", (8183,), ErcIntent.STATE_MACHINE),
        ("ERC-8004 security assumptions", (8004,), ErcIntent.SECURITY),
        ("What is the status of EIP-8004?", (8004,), ErcIntent.STATUS),
        ("Summarize the ERC-8004 discussion", (8004,), ErcIntent.DISCUSSION),
        ("What tests or examples exist for ERC-8004?", (8004,), ErcIntent.IMPLEMENTATION),
        ("Give me an overview of ERC-8004", (8004,), ErcIntent.OVERVIEW),
        (
            "Compare ERC-8183 with eip-8004",
            (8183, 8004),
            ErcIntent.COMPARISON,
        ),
        ("ERC-8004 和 ERC-8183 有什么区别?", (8004, 8183), ErcIntent.COMPARISON),
        ("ERC-8004 是怎么实现的?", (8004,), ErcIntent.IMPLEMENTATION),
    ],
)
def test_planner_extracts_explicit_ercs_and_intent(
    text: str,
    numbers: tuple[int, ...],
    intent: ErcIntent,
) -> None:
    result = ErcQueryPlanner().plan(text)

    assert result is not None
    assert result.erc_numbers == numbers
    assert result.intent is intent


@pytest.mark.parametrize(
    "text",
    [
        "How is 8004 implemented?",
        "Read https://eips.ethereum.org/EIPS/eip-8004",
        "Ask @erc-8004 about this",
        "ERC-0 overview",
        "ERC-100000 overview",
        "merchantERC-8004 overview",
    ],
)
def test_planner_requires_an_explicit_standalone_erc_reference(text: str) -> None:
    assert ErcQueryPlanner().plan(text) is None


def test_planner_deduplicates_ercs_in_first_mention_order() -> None:
    result = ErcQueryPlanner().plan("ERC-8183 vs ERC-8004 and ERC 8183")

    assert result is not None
    assert result.erc_numbers == (8183, 8004)
    assert result.intent is ErcIntent.COMPARISON


def test_planner_rejects_more_than_four_ercs() -> None:
    with pytest.raises(ErcQueryRejected, match="at most four"):
        ErcQueryPlanner().plan("compare ERC-1 ERC-2 ERC-3 ERC-4 ERC-5")
