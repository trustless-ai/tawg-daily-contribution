from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_trusty_page_does_not_invent_an_implementation_stack() -> None:
    page = (ROOT / "knowledge/topics/trusty.md").read_text(encoding="utf-8")
    index = (ROOT / "knowledge/index.md").read_text(encoding="utf-8")

    assert "Anthropic's Claude Agent SDK" not in page
    assert "TAWG group's AI assistant" not in page
    assert "@trustless_ai_bot" not in page
    assert "Trusty is this bot." in page
    assert "[[topics/trusty|Trusty]]" in index
    assert "updated: '2026-08-29'" in index


def test_new_knowledge_policy_forbids_unsourced_implementation_details() -> None:
    policy = (ROOT / "prompts/reply-system.md").read_text(encoding="utf-8")

    assert "A supplied repository URL proves only the repository location" in policy
    assert "Do not add vendor, runtime, SDK, architecture, or implementation claims" in policy
