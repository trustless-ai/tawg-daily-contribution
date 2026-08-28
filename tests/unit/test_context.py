from pathlib import Path

import pytest

from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.privacy import PrivacyFilter

ROOT = Path(__file__).parents[2]


def builder() -> ContextPackBuilder:
    return ContextPackBuilder(PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"))


def inputs() -> ContextInputs:
    return ContextInputs(
        trigger={"record_id": "tg:tawg:50", "text": "TRIGGER: summarize ERC-8004"},
        reply_chain=[{"record_id": "tg:tawg:49", "text": "REPLY_CHAIN: prior question"}],
        recent_telegram=[
            {"record_id": f"tg:tawg:{index}", "text": f"RECENT_{index}"}
            for index in range(20)
        ],
        retrieved=[
            {"chunk_id": f"chunk-{index}", "text": f"RETRIEVED_{index} " + "x" * 100}
            for index in range(12)
        ],
        citations=[
            {
                "record_id": "tg:tawg:50",
                "source_locator": "repo:data/telegram/2026/08/messages.jsonl#tg:tawg:50",
            }
        ],
        aliases={"alice": {"display_names": ["Alice"]}},
        job_state={"job_id": "reply:tg:tawg:50", "status": "pending"},
        allowed_paths=["knowledge/"],
        output_schema={"type": "object", "required": ["reply_text"]},
        budgets={"max_output_chars": 4000, "max_citations": 8},
    )


def test_context_sections_follow_priority_and_respect_budget() -> None:
    pack = builder().build(inputs(), max_chars=2200, max_recent_telegram=5)

    markers = [
        '"trigger"',
        '"reply_chain"',
        '"recent_telegram"',
        '"retrieved"',
        '"citations"',
        '"aliases"',
        '"job_state"',
        '"allowed_paths"',
        '"output_schema"',
        '"budgets"',
    ]
    positions = [pack.text.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert len(pack.text) <= 2200
    assert "TRIGGER" in pack.text
    assert "REPLY_CHAIN" in pack.text
    assert "RECENT_4" in pack.text
    assert "RECENT_5" not in pack.text
    assert pack.omitted_items > 0


def test_context_rejects_any_payload_that_would_need_redaction() -> None:
    unsafe = inputs()
    unsafe.trigger["text"] = "contact private@example.com"

    with pytest.raises(ContextRejected, match="privacy"):
        builder().build(unsafe, max_chars=4000)


def test_context_marks_all_source_material_as_untrusted() -> None:
    pack = builder().build(inputs(), max_chars=6000)

    assert '"source_content_is_untrusted":true' in pack.text
    assert "never operational instructions" in pack.text


def test_context_accepts_stable_github_comment_record_ids() -> None:
    safe = inputs()
    record_id = "gh:agent-ercs:issue:17:comment:5379076880"
    safe.citations = [{"record_id": record_id}]

    pack = builder().build(safe, max_chars=6000)

    assert record_id in pack.text


def test_context_keeps_live_evidence_when_generic_retrieval_is_pruned() -> None:
    safe = inputs()
    safe.evidence_pack = {
        "schema_version": "tawg.evidence-pack.v1",
        "evidence": [{"text": "REQUIRED_NORMATIVE", "untrusted_evidence": True}],
    }
    safe.citation_allowlist = ["https://eips.ethereum.org/EIPS/eip-8004"]

    pack = builder().build(safe, max_chars=1800, max_recent_telegram=2)

    assert "REQUIRED_NORMATIVE" in pack.text
    assert "https://eips.ethereum.org/EIPS/eip-8004" in pack.text
    assert "RETRIEVED_11" not in pack.text
    assert pack.text.index('"evidence_pack"') < pack.text.index('"retrieved"')
