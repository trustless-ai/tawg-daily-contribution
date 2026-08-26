from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.bot_router import BotReplyService, ReplyRejected
from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.live_evidence import (
    EvidenceItem,
    EvidencePack,
    MissingEvidence,
    SourceChange,
)
from tawg_bot.models import PendingBotJob, SourceRecord, SourceType
from tawg_bot.source_registry import EvidenceAuthority, EvidenceKind, SourceRegistry
from tawg_bot.storage import JsonlCollection

PROJECT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
CANONICAL = "https://eips.ethereum.org/EIPS/eip-8004"
IMPLEMENTATION = (
    "https://github.com/trustless-ai/agent-ercs/blob/main/contracts/identity/ERC8004/README.md"
)
ERC_8281_IMPLEMENTATION = (
    "https://github.com/trustless-ai/agent-ercs/blob/main/contracts/verify/ERC8281/README.md"
)


class FakeAi:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.result)


class FakeLiveEvidence:
    def __init__(self, pack: EvidencePack) -> None:
        self.pack = pack
        self.calls: list[ErcQuery] = []

    async def build(self, query: ErcQuery, *, now: datetime) -> EvidencePack:
        assert now >= NOW
        self.calls.append(query)
        return self.pack.model_copy(update={"query": query})


def _pack(*, missing_normative: bool = False) -> EvidencePack:
    evidence = []
    for source_key, kind, authority, url, text in (
        (
            "erc-8004-canonical",
            EvidenceKind.NORMATIVE_SPEC,
            EvidenceAuthority.CANONICAL,
            CANONICAL,
            "normative interface",
        ),
        (
            "agent-ercs-8004-readme",
            EvidenceKind.IMPLEMENTATION,
            EvidenceAuthority.OFFICIAL_ORG,
            IMPLEMENTATION,
            "named implementation",
        ),
    ):
        if missing_normative and kind is EvidenceKind.NORMATIVE_SPEC:
            continue
        evidence.append(
            EvidenceItem(
                erc_number=8004,
                source_key=source_key,
                kind=kind,
                authority=authority,
                canonical_url=url,
                citation_url=url,
                observed_at=NOW,
                version="v2",
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                source_byte_count=len(text),
                excerpted=False,
                text=text,
            )
        )
    missing = (
        [
            MissingEvidence(
                erc_number=8004,
                intent=ErcIntent.IMPLEMENTATION,
                bucket="normative_spec",
                source_key="erc-8004-canonical",
                safe_error_code="timeout",
            )
        ]
        if missing_normative
        else []
    )
    changes = [
        SourceChange(
            erc_number=8004,
            source_key=item.source_key,
            previous_version=None,
            observed_version=item.version,
            previous_sha256=None,
            observed_sha256=item.content_sha256,
            observed_at=NOW,
        )
        for item in evidence
    ]
    return EvidencePack(
        query=ErcQuery(erc_numbers=(8004,), intent=ErcIntent.IMPLEMENTATION),
        generated_orientation=[],
        evidence=evidence,
        citation_allowlist=[item.citation_url for item in evidence],
        missing_required=missing,
        source_changes=changes,
    )


def _result(
    *,
    citations: list[str],
    status: str = "verified",
    gaps: list[str] | None = None,
    language: str = "en",
) -> dict[str, Any]:
    return {
        "schema_version": "tawg.reply-result.v2",
        "reply_text": "The current evidence supports this answer.",
        "language": language,
        "english_recap": None,
        "citations": citations,
        "evidence_status": status,
        "verification_gaps": gaps or [],
        "correction_transaction": None,
        "refusal": False,
    }


def _seed(root: Path, text: str) -> PendingBotJob:
    for relative in (
        "config/privacy.yml",
        "knowledge/meta/aliases.yml",
        "knowledge/meta/sources.yml",
        "knowledge/meta/source-ledger.json",
        "knowledge/meta/claim-ledger.json",
        "src/tawg_bot/schemas/reply-result.v2.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    (root / "knowledge/index.md").write_text(
        "---\ntitle: Index\ntype: index\ncreated: 2026-08-23\nupdated: 2026-08-23\n"
        "---\n\n# Index\n",
        encoding="utf-8",
    )
    record = SourceRecord.from_text(
        record_id="tg:tawg:100",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:100",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=NOW,
        updated_at=NOW,
        text_original=text,
        ingested_at=NOW,
    )
    telegram = root / "data/telegram/2026/08/messages.jsonl"
    telegram.parent.mkdir(parents=True)
    telegram.write_bytes(JsonlCollection(telegram, SourceRecord).merged_bytes([record]))
    state = root / "data/state"
    state.mkdir(parents=True)
    for name in (
        "knowledge-gaps.json",
        "pending-knowledge-refresh.json",
        "source-candidates.json",
    ):
        (state / name).write_text("[]\n", encoding="utf-8")
    job = PendingBotJob(
        job_id="reply:tg:tawg:100",
        trigger_record_id=record.record_id,
        reply_to_message_id=100,
        created_at=NOW,
        updated_at=NOW,
    )
    (state / "pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n", encoding="utf-8"
    )
    return job


def _service(
    root: Path,
    *,
    ai: FakeAi,
    live: FakeLiveEvidence,
) -> BotReplyService:
    registry = SourceRegistry.from_yaml(root / "knowledge/meta/sources.yml")
    return BotReplyService(
        root,
        ai=ai,
        bot_username="bot",
        live_evidence=live,
        knowledge_state=KnowledgeStateStore(root, registry=registry),
    )


@pytest.mark.asyncio
async def test_ordinary_erc_reply_reuses_local_knowledge_without_live_fetch(
    tmp_path: Path,
) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    page = tmp_path / "knowledge/ercs/erc-8004.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes((PROJECT / "knowledge/ercs/erc-8004.md").read_bytes())
    ai = FakeAi(_result(citations=[CANONICAL, IMPLEMENTATION]))
    live = FakeLiveEvidence(_pack())

    prepared = await _service(tmp_path, ai=ai, live=live).prepare(
        job.job_id, now=NOW + timedelta(minutes=1)
    )

    context = ai.calls[0]["context_pack"]
    decoded = json.loads(context)
    assert '"schema_version":"tawg.evidence-pack.v1"' not in context
    assert decoded["trigger"]["erc_evidence_mode"] == "local_synthesis"
    assert decoded["trigger"]["local_verified_at"] == ["2026-08-23T06:55:28.909924Z"]
    assert decoded["retrieved"][0]["path"] == "knowledge/ercs/erc-8004.md"
    assert "knowledge/ercs/erc-8004.md" in context
    assert CANONICAL in context
    assert prepared.citations == (CANONICAL, IMPLEMENTATION)
    assert live.calls == []


@pytest.mark.asyncio
async def test_local_erc_correction_context_exposes_exact_current_page_revision(
    tmp_path: Path,
) -> None:
    job = _seed(tmp_path, "@bot Please add OCP to your knowledge, which is ERC 8281")
    shutil.copytree(PROJECT / "knowledge", tmp_path / "knowledge", dirs_exist_ok=True)
    current = (PROJECT / "knowledge/ercs/erc-8281.md").read_text(encoding="utf-8")
    ai = FakeAi(
        {
            **_result(citations=["tg:tawg:100"]),
            "reply_text": "I need stronger evidence before changing the page. [tg:tawg:100]",
            "evidence_status": "not_verified",
            "verification_gaps": ["The trigger does not establish the acronym."],
        }
    )

    await _service(tmp_path, ai=ai, live=FakeLiveEvidence(_pack())).prepare(
        job.job_id, now=NOW + timedelta(minutes=1)
    )

    context = json.loads(ai.calls[0]["context_pack"])
    retrieved = context["retrieved"][0]
    assert retrieved["path"] == "knowledge/ercs/erc-8281.md"
    assert retrieved["expected_sha256"] == hashlib.sha256(current.encode()).hexdigest()
    assert retrieved["text"] == current


@pytest.mark.asyncio
async def test_local_erc_correction_persists_against_v2_evidence_frontmatter(
    tmp_path: Path,
) -> None:
    job = _seed(tmp_path, "@bot Please add OCP to your knowledge, which is ERC 8281")
    shutil.copytree(PROJECT / "knowledge", tmp_path / "knowledge", dirs_exist_ok=True)
    page = tmp_path / "knowledge/ercs/erc-8281.md"
    current = (PROJECT / "knowledge/ercs/erc-8281.md").read_text(encoding="utf-8")
    corrected = current.replace(
        "telegram_record_ids: []",
        "telegram_record_ids:\n- tg:tawg:100",
    ).replace(
        "The agent-ercs implementation draft provides",
        "OCP means Observation Commitment Protocol. The agent-ercs implementation draft provides",
    )
    result = {
        **_result(citations=["tg:tawg:100"]),
        "reply_text": "Added the evidence-backed OCP expansion. [tg:tawg:100]",
        "correction_transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": job.job_id,
            "writes": [
                {
                    "path": "knowledge/ercs/erc-8281.md",
                    "expected_sha256": hashlib.sha256(current.encode()).hexdigest(),
                    "content": corrected,
                    "citations": [
                        "agent-ercs-8281-implementation",
                        "tg:tawg:100",
                        ERC_8281_IMPLEMENTATION,
                    ],
                }
            ],
        },
    }

    prepared = await _service(
        tmp_path,
        ai=FakeAi(result),
        live=FakeLiveEvidence(_pack()),
    ).prepare(job.job_id, now=NOW + timedelta(minutes=1))

    assert "Added" in prepared.reply_text
    assert page.read_text(encoding="utf-8") == corrected


@pytest.mark.asyncio
async def test_erc_reply_fetches_live_when_local_knowledge_is_missing(tmp_path: Path) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    ai = FakeAi(_result(citations=[CANONICAL, IMPLEMENTATION]))
    live = FakeLiveEvidence(_pack())

    prepared = await _service(tmp_path, ai=ai, live=live).prepare(
        job.job_id, now=NOW + timedelta(minutes=1)
    )

    context = ai.calls[0]["context_pack"]
    assert '"schema_version":"tawg.evidence-pack.v1"' in context
    assert "normative interface" in context
    assert prepared.citations == (CANONICAL, IMPLEMENTATION)
    assert live.calls[0].intent is ErcIntent.IMPLEMENTATION
    state = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()
    assert len(state.refresh_jobs) == 2


@pytest.mark.asyncio
async def test_explicitly_current_erc_reply_fetches_live_even_with_local_knowledge(
    tmp_path: Path,
) -> None:
    job = _seed(tmp_path, "@bot What is the current status of ERC-8004?")
    page = tmp_path / "knowledge/ercs/erc-8004.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes((PROJECT / "knowledge/ercs/erc-8004.md").read_bytes())
    ai = FakeAi(_result(citations=[CANONICAL, IMPLEMENTATION]))
    live = FakeLiveEvidence(_pack())

    await _service(tmp_path, ai=ai, live=live).prepare(
        job.job_id, now=NOW + timedelta(minutes=1)
    )

    assert live.calls[0].intent is ErcIntent.STATUS


@pytest.mark.asyncio
async def test_registered_but_unfetched_url_is_rejected(tmp_path: Path) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    failed_url = "https://ethereum-magicians.org/t/erc-8004-trustless-agents/25098"
    ai = FakeAi(_result(citations=[CANONICAL, failed_url]))

    with pytest.raises(ReplyRejected, match="safely"):
        await _service(tmp_path, ai=ai, live=FakeLiveEvidence(_pack())).prepare(
            job.job_id, now=NOW + timedelta(minutes=1)
        )

    pending = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )[0]
    assert pending["status"] == "pending"


@pytest.mark.asyncio
async def test_markdown_link_label_cannot_hide_an_undeclared_local_citation(
    tmp_path: Path,
) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    output = _result(citations=[IMPLEMENTATION])
    output["reply_text"] = f"See [tg:tawg:999]({IMPLEMENTATION})."

    with pytest.raises(ReplyRejected, match="safely"):
        await _service(
            tmp_path,
            ai=FakeAi(output),
            live=FakeLiveEvidence(_pack()),
        ).prepare(job.job_id, now=NOW + timedelta(minutes=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("overstated_status", ["verified", "partial"])
async def test_missing_normative_source_rejects_overstated_status(
    tmp_path: Path, overstated_status: str
) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    ai = FakeAi(
        _result(citations=[IMPLEMENTATION], status=overstated_status, gaps=["normative_spec"])
    )

    with pytest.raises(ReplyRejected, match="safely"):
        await _service(
            tmp_path,
            ai=ai,
            live=FakeLiveEvidence(_pack(missing_normative=True)),
        ).prepare(job.job_id, now=NOW + timedelta(minutes=1))

    state = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()
    assert state.gaps[0].bucket == "normative_spec"


@pytest.mark.asyncio
async def test_missing_normative_source_accepts_not_verified_with_gap(tmp_path: Path) -> None:
    job = _seed(tmp_path, "@bot How is ERC-8004 implemented?")
    ai = FakeAi(
        _result(
            citations=[IMPLEMENTATION],
            status="not_verified",
            gaps=["normative_spec source timed out"],
        )
    )

    prepared = await _service(
        tmp_path,
        ai=ai,
        live=FakeLiveEvidence(_pack(missing_normative=True)),
    ).prepare(job.job_id, now=NOW + timedelta(minutes=1))

    assert prepared.reply_text == (
        "The current evidence supports this answer.\n\n"
        f"Sources:\n• {IMPLEMENTATION}"
    )
    assert prepared.citations == (IMPLEMENTATION,)


@pytest.mark.asyncio
async def test_source_suggestion_is_stored_without_live_fetch(tmp_path: Path) -> None:
    job = _seed(
        tmp_path,
        "@bot source suggestion: https://ethereum-magicians.org/t/new-proposal/999 "
        "and https://github.com/trustless-ai/agent-ercs/issues/42",
    )
    ai = FakeAi(_result(citations=[]))
    live = FakeLiveEvidence(_pack())

    await _service(tmp_path, ai=ai, live=live).prepare(job.job_id, now=NOW + timedelta(minutes=1))

    assert live.calls == []
    state = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()
    assert {item.url for item in state.candidates} == {
        "https://ethereum-magicians.org/t/new-proposal/999",
        "https://github.com/trustless-ai/agent-ercs/issues/42",
    }
