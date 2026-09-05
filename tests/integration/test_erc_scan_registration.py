from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.bot_router import BotReplyService
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.scan_targets import (
    ErcScanTarget,
    ScanRegistrationProposal,
    ScanTargetRejected,
    ScanTargetStore,
    ScanTargetVerifier,
)
from tawg_bot.source_registry import SourceRegistry
from tests.integration.test_bot_replies import NOW, FakeAi, seed

MAGICIANS = "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902"
PROPOSAL_PR = "https://github.com/ethereum/ERCs/pull/1081"
PROJECT = Path(__file__).parents[2]


class FakeVerifier:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[ScanRegistrationProposal] = []

    async def verify(
        self,
        proposal: ScanRegistrationProposal,
        *,
        trigger_record_id: str,
        now,
    ) -> ErcScanTarget:
        self.calls.append(proposal)
        if self.reject:
            raise ScanTargetRejected("scan target metadata does not match the ERC")
        return ErcScanTarget(
            **proposal.model_dump(),
            registered_from_record_id=trigger_record_id,
            registered_at=now,
        )


class TopicClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.paths: list[str] = []

    async def get_json(self, path: str, params=None) -> dict[str, Any]:
        del params
        self.paths.append(path)
        return self.payload


class GitHubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.paths: list[str] = []

    async def get_json(self, path: str, params=None) -> dict[str, Any]:
        del params
        self.paths.append(path)
        return self.payload


def _seed_registry(root: Path) -> None:
    target = root / ScanTargetStore.PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "schema: tawg.scan-targets.v1\n"
        "github_organization: trustless-ai\n"
        "include_public_archived_repositories: true\n"
        "ercs: []\n",
        encoding="utf-8",
    )


def _seed_knowledge_state(root: Path) -> KnowledgeStateStore:
    sources = root / "knowledge/meta/sources.yml"
    sources.parent.mkdir(parents=True, exist_ok=True)
    sources.write_bytes((PROJECT / "knowledge/meta/sources.yml").read_bytes())
    return KnowledgeStateStore(root, registry=SourceRegistry.from_yaml(sources))


def _registration_result(
    job_id: str,
    trigger_id: str,
    *,
    proposal_pr_url: str | None,
) -> dict[str, Any]:
    urls = [MAGICIANS, *([proposal_pr_url] if proposal_pr_url is not None else [])]
    page = (
        "---\n"
        "title: Agentic Commerce\n"
        "type: concept\n"
        "created: '2026-08-28'\n"
        "updated: '2026-08-28'\n"
        "source_ids:\n"
        f"- {trigger_id}\n"
        "source_urls:\n"
        + "".join(f"- {url}\n" for url in urls)
        + "provenance_status: verified\n"
        "---\n\n"
        "# Agentic Commerce\n\n"
        "A brief external description.\n\n"
        "## Sources\n\n"
        + "".join(f"- {url}\n" for url in urls)
    )
    citations = [trigger_id, *urls]
    return {
        "schema_version": "tawg.reply-result.v3",
        "reply_text": "Recorded Agentic Commerce and its scan request.",
        "language": "en",
        "english_recap": None,
        "citations": citations,
        "evidence_status": "verified",
        "verification_gaps": [],
        "correction_transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": job_id,
            "writes": [
                {
                    "path": "knowledge/topics/agentic-commerce.md",
                    "expected_sha256": None,
                    "content": page,
                    "citations": citations,
                }
            ],
        },
        "knowledge_write": {
            "authorship": "external",
            "authorship_evidence": [trigger_id],
            "original_url": MAGICIANS,
        },
        "scan_registration": {
            "erc_number": 8183,
            "magicians_topic_url": MAGICIANS,
            "proposal_pr_url": proposal_pr_url,
        },
        "refusal": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_pr_url", [None, PROPOSAL_PR])
async def test_complete_erc_registration_is_staged_atomically(
    tmp_path: Path,
    proposal_pr_url: str | None,
) -> None:
    urls = f"{MAGICIANS} {proposal_pr_url or ''}".strip()
    job = seed(tmp_path, f"@bot record ERC-8183 and scan {urls}")
    _seed_registry(tmp_path)
    verifier = FakeVerifier()

    prepared = await BotReplyService(
        tmp_path,
        ai=FakeAi(
            _registration_result(
                job.job_id,
                job.trigger_record_id,
                proposal_pr_url=proposal_pr_url,
            ),
            route="knowledge_correction",
        ),
        bot_username="bot",
        scan_target_verifier=verifier,
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert prepared.refusal is False
    target = ScanTargetStore(tmp_path).load().ercs[0]
    assert target.erc_number == 8183
    assert target.proposal_pr_url == proposal_pr_url
    assert target.registered_from_record_id == job.trigger_record_id
    assert (tmp_path / "knowledge/topics/agentic-commerce.md").is_file()


@pytest.mark.asyncio
async def test_invalid_registration_does_not_block_valid_knowledge(tmp_path: Path) -> None:
    job = seed(tmp_path, f"@bot record ERC-8183 and scan {MAGICIANS}")
    _seed_registry(tmp_path)

    prepared = await BotReplyService(
        tmp_path,
        ai=FakeAi(
            _registration_result(
                job.job_id,
                job.trigger_record_id,
                proposal_pr_url=None,
            ),
            route="knowledge_correction",
        ),
        bot_username="bot",
        scan_target_verifier=FakeVerifier(reject=True),
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert "recurring scan was not registered" in prepared.reply_text
    assert (tmp_path / "knowledge/topics/agentic-commerce.md").is_file()
    assert ScanTargetStore(tmp_path).load().ercs == []
    state = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )[0]
    assert state["status"] == "ready"


@pytest.mark.asyncio
async def test_scan_registration_accepts_magicians_url_with_post_id(tmp_path: Path) -> None:
    """A knowledge-correction reply whose ``scan_registration`` echoes the raw Magicians link
    with a trailing post id (``/27902/17``) must be normalized to the canonical topic URL
    instead of aborting the whole reply as an "invalid reply model output"."""
    post_id_url = f"{MAGICIANS}/17"
    job = seed(tmp_path, f"@bot record ERC-8183 and scan {MAGICIANS}")
    _seed_registry(tmp_path)
    verifier = FakeVerifier()

    result = _registration_result(
        job.job_id,
        job.trigger_record_id,
        proposal_pr_url=None,
    )
    result["scan_registration"]["magicians_topic_url"] = post_id_url

    prepared = await BotReplyService(
        tmp_path,
        ai=FakeAi(result, route="knowledge_correction"),
        bot_username="bot",
        scan_target_verifier=verifier,
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert prepared.refusal is False
    targets = ScanTargetStore(tmp_path).load().ercs
    assert len(targets) == 1
    assert targets[0].magicians_topic_url == MAGICIANS
    assert verifier.calls and verifier.calls[0].magicians_topic_url == MAGICIANS


@pytest.mark.asyncio
async def test_source_suggestion_registers_erc_scan_target(tmp_path: Path) -> None:
    """A source suggestion that names exactly one ERC plus its Magicians topic (and an
    optional proposal PR) must register the ERC scan target, not just store source
    candidates. The Discourse post id in the URL is normalised away before the registry
    write."""
    text = (
        "@bot please add ERC 8380 to your ERC follow up list. "
        "Magician: https://ethereum-magicians.org/t/"
        "erc-8380-unclonable-agent-execution-credentials/29274/17 "
        "Proposal: https://github.com/ethereum/ERCs/pull/1953"
    )
    job = seed(tmp_path, text)
    _seed_registry(tmp_path)
    knowledge_state = _seed_knowledge_state(tmp_path)
    verifier = FakeVerifier()
    normalized = (
        "https://ethereum-magicians.org/t/"
        "erc-8380-unclonable-agent-execution-credentials/29274"
    )
    result = {
        "schema_version": "tawg.reply-result.v3",
        "reply_text": "Recorded ERC-8380 as a recurring scan target.",
        "language": "en",
        "english_recap": None,
        "citations": [],
        "evidence_status": "verified",
        "verification_gaps": [],
        "correction_transaction": None,
        "knowledge_write": None,
        "scan_registration": None,
        "refusal": False,
    }

    prepared = await BotReplyService(
        tmp_path,
        ai=FakeAi(result, route="source_suggestion"),
        bot_username="bot",
        scan_target_verifier=verifier,
        knowledge_state=knowledge_state,
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert prepared.refusal is False
    targets = ScanTargetStore(tmp_path).load().ercs
    assert len(targets) == 1
    assert targets[0].erc_number == 8380
    assert targets[0].magicians_topic_url == normalized
    assert targets[0].proposal_pr_url == "https://github.com/ethereum/ERCs/pull/1953"
    assert targets[0].registered_from_record_id == job.trigger_record_id
    assert [call.erc_number for call in verifier.calls] == [8380]


@pytest.mark.asyncio
async def test_registration_urls_must_come_from_the_current_audited_chain(
    tmp_path: Path,
) -> None:
    job = seed(tmp_path, "@bot record ERC-8183 for future scanning")
    _seed_registry(tmp_path)

    with pytest.raises(ValueError, match="safely"):
        await BotReplyService(
            tmp_path,
            ai=FakeAi(
                _registration_result(
                    job.job_id,
                    job.trigger_record_id,
                    proposal_pr_url=None,
                ),
                route="knowledge_correction",
            ),
            bot_username="bot",
            scan_target_verifier=FakeVerifier(),
        ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert not (tmp_path / "knowledge/topics/agentic-commerce.md").exists()


@pytest.mark.asyncio
async def test_verifier_resolves_exact_topic_and_optional_proposal_pr() -> None:
    topic = TopicClient(
        {"id": 27902, "slug": "erc-8183-agentic-commerce", "title": "ERC-8183"}
    )
    github = GitHubClient(
        {"number": 1081, "title": "Add ERC-8183", "body": "Agentic commerce"}
    )
    verifier = ScanTargetVerifier(topic_client=topic, github_client=github)

    target = await verifier.verify(
        ScanRegistrationProposal(
            erc_number=8183,
            magicians_topic_url=MAGICIANS,
            proposal_pr_url=PROPOSAL_PR,
        ),
        trigger_record_id="tg:tawg:6000",
        now=NOW,
    )

    assert target.erc_number == 8183
    assert topic.paths == ["/t/27902.json"]
    assert github.paths == ["/repos/ethereum/ERCs/pulls/1081"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topic_payload",
    [
        {"id": 7, "slug": "erc-8183-agentic-commerce", "title": "ERC-8183"},
        {"id": 27902, "slug": "different", "title": "ERC-8183"},
        {"id": 27902, "slug": "erc-8183-agentic-commerce", "title": "Other topic"},
    ],
)
async def test_verifier_rejects_mismatched_topic_metadata(
    topic_payload: dict[str, Any],
) -> None:
    verifier = ScanTargetVerifier(
        topic_client=TopicClient(topic_payload),
        github_client=None,
    )

    with pytest.raises(ScanTargetRejected):
        await verifier.verify(
            ScanRegistrationProposal(
                erc_number=8183,
                magicians_topic_url=MAGICIANS,
                proposal_pr_url=None,
            ),
            trigger_record_id="tg:tawg:6000",
            now=NOW,
        )
