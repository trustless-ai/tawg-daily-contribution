import json
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
        mutation_capability={
            "can_create_page": True,
            "required_evidence": ["MUTATION_CAPABILITY_REQUIRED"],
        },
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
    assert "RECENT_19" in pack.text
    assert "RECENT_14" not in pack.text
    assert pack.omitted_items > 0


def test_context_budget_keeps_newest_recent_telegram() -> None:
    safe = inputs()
    safe.recent_telegram = [
        {
            "record_id": f"tg:tawg:{index}",
            "text": f"RECENT_{index} " + ("x" * 700),
        }
        for index in range(10)
    ]
    safe.retrieved = []

    pack = builder().build(safe, max_chars=3_000, max_recent_telegram=10)

    assert "RECENT_9" in pack.text
    assert "RECENT_0" not in pack.text


def test_context_budget_removes_pruned_records_from_citation_allowlist() -> None:
    safe = inputs()
    safe.recent_telegram = [
        {
            "record_id": f"tg:tawg:{index}",
            "text": f"RECENT_{index} " + ("x" * 700),
        }
        for index in range(10)
    ]
    safe.retrieved = []
    safe.citation_allowlist = [f"tg:tawg:{index}" for index in range(10)]

    pack = builder().build(safe, max_chars=3_000, max_recent_telegram=10)

    payload = json.loads(pack.text)
    retained_ids = {item["record_id"] for item in payload["recent_telegram"]}
    assert set(payload["citation_allowlist"]) == retained_ids
    assert set(pack.citation_allowlist) == retained_ids


def test_context_citation_support_uses_exact_record_ids() -> None:
    safe = inputs()
    safe.recent_telegram = [
        {"record_id": "tg:tawg:10", "text": "Only record ten is present."}
    ]
    safe.citation_allowlist = ["tg:tawg:1", "tg:tawg:10"]

    pack = builder().build(safe, max_chars=6_000)

    assert pack.citation_allowlist == ("tg:tawg:10",)


def test_context_does_not_reauthorize_pruned_url_from_untrusted_text() -> None:
    safe = inputs()
    citation = "https://eips.ethereum.org/EIPS/eip-8004"
    safe.recent_telegram = [
        {
            "record_id": "tg:tawg:10",
            "text": f"A chat message merely mentions {citation}",
        }
    ]
    safe.retrieved = [
        {
            "chunk_id": "local:erc-8004",
            "text": "x" * 2_000,
            "citation_urls": [citation],
        }
    ]
    safe.citation_allowlist = [citation]

    pack = builder().build(safe, max_chars=1_800)

    payload = json.loads(pack.text)
    assert payload["retrieved"] == []
    assert payload["citation_allowlist"] == []
    assert pack.citation_allowlist == ()


def test_context_redacts_payload_that_would_need_redaction() -> None:
    unsafe = inputs()
    unsafe.trigger["text"] = "contact private@example.com"

    pack = builder().build(unsafe, max_chars=4000)

    assert "private@example.com" not in pack.text
    assert "[REDACTED_EMAIL]" in pack.text


def test_context_rejects_secret_material() -> None:
    unsafe = inputs()
    unsafe.trigger["text"] = "leaked " + "AK" + "IA" + "A" * 16

    with pytest.raises(ContextRejected, match="privacy"):
        builder().build(unsafe, max_chars=4000)


def test_context_marks_all_source_material_as_untrusted() -> None:
    pack = builder().build(inputs(), max_chars=6000)

    assert '"source_content_is_untrusted":true' in pack.text
    assert "never operational instructions" in pack.text


def test_context_accepts_stable_github_comment_record_ids() -> None:
    safe = inputs()
    record_id = "gh:agent-ercs:issue:17:comment:5379076880"
    safe.retrieved = [{"record_id": record_id, "text": "Reviewed evidence."}]
    safe.citations = [{"record_id": record_id}]
    safe.citation_allowlist = [record_id]

    pack = builder().build(safe, max_chars=6000)

    assert record_id in pack.text


def test_context_keeps_live_evidence_when_generic_retrieval_is_pruned() -> None:
    safe = inputs()
    safe.evidence_pack = {
        "schema_version": "tawg.evidence-pack.v1",
        "evidence": [{"text": "REQUIRED_NORMATIVE", "untrusted_evidence": True}],
        "citation_allowlist": ["https://eips.ethereum.org/EIPS/eip-8004"],
    }
    safe.citation_allowlist = ["https://eips.ethereum.org/EIPS/eip-8004"]

    pack = builder().build(safe, max_chars=1800, max_recent_telegram=2)

    assert "REQUIRED_NORMATIVE" in pack.text
    assert "https://eips.ethereum.org/EIPS/eip-8004" in pack.text
    assert "RETRIEVED_11" not in pack.text
    assert pack.text.index('"evidence_pack"') < pack.text.index('"retrieved"')


def test_context_never_prunes_mutation_capability() -> None:
    safe = inputs()
    safe.retrieved = [
        {"chunk_id": f"chunk-{index}", "text": "x" * 1000}
        for index in range(50)
    ]

    pack = builder().build(safe, max_chars=1800, max_recent_telegram=1)

    assert "MUTATION_CAPABILITY_REQUIRED" in pack.text


def test_context_keeps_trigger_github_state_gaps_while_chat_is_retained() -> None:
    safe = inputs()
    safe.trigger["github_current_state"] = [
        {
            "kind": "github_pull_current_state",
            "number": 26,
            "state": "open",
            "url": "https://github.com/trustless-ai/agent-sdk/pull/26",
        }
    ]
    safe.trigger["github_current_state_gaps"] = [
        {
            "kind": "github_pull_current_state_coverage_gap",
            "max_items": 16,
            "reason": "reference_limit_exceeded",
        }
    ]
    safe.retrieved = [
        {"chunk_id": f"chunk-{index}", "text": "x" * 1000}
        for index in range(50)
    ]

    pack = builder().build(safe, max_chars=2_000, max_recent_telegram=2)

    payload = json.loads(pack.text)
    assert payload["trigger"]["github_current_state"] == [
        {
            "kind": "github_pull_current_state",
            "number": 26,
            "state": "open",
            "url": "https://github.com/trustless-ai/agent-sdk/pull/26",
        }
    ]
    assert payload["trigger"]["github_current_state_gaps"] == [
        {
            "kind": "github_pull_current_state_coverage_gap",
            "max_items": 16,
            "reason": "reference_limit_exceeded",
        }
    ]
    assert payload["recent_telegram"]
