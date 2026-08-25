from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.knowledge_jobs import KnowledgeRefreshJob, refresh_key
from tawg_bot.knowledge_refresh import KnowledgeRefresh, KnowledgeRefreshRejected
from tawg_bot.live_evidence import EvidenceItem, EvidencePack, MissingEvidence
from tawg_bot.source_registry import EvidenceAuthority, EvidenceKind, SourceRegistry
from tawg_bot.unit_of_work import RepositoryUnitOfWork

PROJECT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
JOB_KEY = "refresh:erc-8004:erc-8004-canonical:0123456789abcdef"
CANONICAL = "https://eips.ethereum.org/EIPS/eip-8004"
IMPLEMENTATION = (
    "https://github.com/trustless-ai/agent-ercs/blob/main/contracts/identity/ERC8004/README.md"
)
CANONICAL_TEXT = (
    "The normative registry interface defines agent identity and interoperable discovery "
    "requirements without prescribing one deployment architecture."
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
        assert now == NOW
        self.calls.append(query)
        return self.pack.model_copy(update={"query": query})


def _item(
    source_key: str,
    kind: EvidenceKind,
    authority: EvidenceAuthority,
    url: str,
    text: str,
) -> EvidenceItem:
    return EvidenceItem(
        erc_number=8004,
        source_key=source_key,
        kind=kind,
        authority=authority,
        canonical_url=url,
        citation_url=url,
        observed_at=NOW,
        version="fixture-v1",
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_byte_count=len(text.encode()),
        excerpted=False,
        text=text,
    )


def _pack() -> EvidencePack:
    items = [
        _item(
            "erc-8004-canonical",
            EvidenceKind.NORMATIVE_SPEC,
            EvidenceAuthority.CANONICAL,
            CANONICAL,
            CANONICAL_TEXT,
        ),
        _item(
            "agent-ercs-8004-readme",
            EvidenceKind.IMPLEMENTATION,
            EvidenceAuthority.OFFICIAL_ORG,
            IMPLEMENTATION,
            "The maintained implementation maps the interface to deployable contracts.",
        ),
    ]
    return EvidencePack(
        query=ErcQuery(erc_numbers=(8004,), intent=ErcIntent.IMPLEMENTATION),
        generated_orientation=[],
        evidence=items,
        citation_allowlist=[item.citation_url for item in items],
        missing_required=[
            MissingEvidence(
                erc_number=8004,
                intent=ErcIntent.IMPLEMENTATION,
                bucket="test_or_example",
                source_key=None,
                safe_error_code="unregistered_source",
            )
        ],
        source_changes=[],
    )


def _seed(root: Path) -> SourceRegistry:
    for relative in (
        "config/privacy.yml",
        "knowledge/meta/sources.yml",
        "src/tawg_bot/schemas/knowledge-result.v2.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if (PROJECT / relative).exists():
            target.write_bytes((PROJECT / relative).read_bytes())
    meta = root / "knowledge/meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    (meta / "source-ledger.json").write_text(
        '{"schema":"tawg.source-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    (meta / "claim-ledger.json").write_text(
        '{"schema":"tawg.claim-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    state = root / "data/state"
    state.mkdir(parents=True)
    job = KnowledgeRefreshJob(
        job_key=JOB_KEY,
        erc_number=8004,
        source_key="erc-8004-canonical",
        observed_version="fixture-v1",
        observed_sha256="0" * 64,
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5),
    )
    (state / "pending-knowledge-refresh.json").write_text(
        json.dumps([job.model_dump(mode="json")]) + "\n", encoding="utf-8"
    )
    (state / "knowledge-gaps.json").write_text("[]\n", encoding="utf-8")
    (state / "source-candidates.json").write_text("[]\n", encoding="utf-8")
    return SourceRegistry.from_yaml(meta / "sources.yml")


def _page() -> str:
    headings = (
        "Summary",
        "Status",
        "Motivation",
        "Architecture",
        "Interfaces",
        "State machine",
        "Implementation",
        "Security considerations",
        "Testing and examples",
        "Open questions",
    )
    body = "\n\n".join(f"## {heading}\n\nCurrent generated synthesis." for heading in headings)
    return (
        "---\n"
        "title: ERC-8004\n"
        "type: erc\n"
        "created: 2026-08-23\n"
        "updated: 2026-08-23\n"
        "verified_at: 2026-08-23T05:00:00Z\n"
        "source_keys:\n"
        "  - erc-8004-canonical\n"
        "  - agent-ercs-8004-readme\n"
        "telegram_record_ids: []\n"
        "---\n\n"
        "# ERC-8004\n\n"
        f"{body}\n\n"
        f"Reliable sources: [canonical]({CANONICAL}), [implementation]({IMPLEMENTATION}).\n"
    )


def _result(root: Path, operation_id: str) -> dict[str, Any]:
    page = _page()
    source_entries: dict[str, Any] = {}
    for item in _pack().evidence:
        source_entries[item.source_key] = {
            "source_kind": item.kind.value,
            "authority": item.authority.value,
            "canonical_url": item.canonical_url,
            "observed_version": item.version,
            "observed_sha256": item.content_sha256,
            "observed_at": item.observed_at.isoformat().replace("+00:00", "Z"),
            "independence_key": item.canonical_url.split("/", 3)[2],
            "active": True,
        }
    source_ledger = (
        json.dumps(
            {"schema": "tawg.source-ledger.v2", "entries": source_entries},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    claim_ledger = (
        json.dumps(
            {
                "schema": "tawg.claim-ledger.v2",
                "entries": {
                    "erc-8004-interface": {
                        "claim_kind": "normative",
                        "state": "accepted",
                        "risk": "ordinary",
                        "source_keys": ["erc-8004-canonical"],
                        "assessed_at": "2026-08-23T05:00:00Z",
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    writes = []
    for path, content, citations in (
        (
            "knowledge/ercs/erc-8004.md",
            page,
            [
                "erc-8004-canonical",
                "agent-ercs-8004-readme",
                CANONICAL,
                IMPLEMENTATION,
            ],
        ),
        ("knowledge/meta/source-ledger.json", source_ledger, []),
        ("knowledge/meta/claim-ledger.json", claim_ledger, []),
    ):
        target = root / path
        writes.append(
            {
                "path": path,
                "expected_sha256": (
                    hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
                ),
                "content": content,
                "citations": citations,
            }
        )
    return {
        "schema_version": "tawg.knowledge-result.v2",
        "processed_job_keys": [JOB_KEY],
        "evidence_gaps": [
            {
                "erc_number": 8004,
                "bucket": "test_or_example",
                "source_key": None,
                "safe_error_code": "unregistered_source",
            }
        ],
        "transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": operation_id,
            "writes": writes,
        },
    }


def _service(
    root: Path,
    registry: SourceRegistry,
    ai: FakeAi,
    live: FakeLiveEvidence,
) -> KnowledgeRefresh:
    return KnowledgeRefresh(root, ai=ai, live_evidence=live, registry=registry)


def test_context_redacts_personal_data_from_transient_live_evidence(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    pack = _pack()
    original_text = (
        "The public specification lists author@example.com and +1 (415) 555-0123 "
        "alongside its normative requirements."
    )
    evidence = list(pack.evidence)
    evidence[0] = evidence[0].model_copy(update={"text": original_text})
    pack = pack.model_copy(update={"evidence": evidence})
    service = _service(
        tmp_path,
        registry,
        FakeAi(_result(tmp_path, "privacy-context")),
        FakeLiveEvidence(pack),
    )

    context = service._context(
        service._pending_jobs(NOW, None),
        (pack,),
        NOW,
        "privacy-context",
    )

    assert "author@example.com" not in context
    assert "+1 (415) 555-0123" not in context
    assert "[REDACTED_EMAIL]" in context
    assert "[REDACTED_PHONE]" in context
    assert pack.evidence[0].text == original_text


def test_context_rejects_secret_material_from_transient_live_evidence(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    pack = _pack()
    evidence = list(pack.evidence)
    evidence[0] = evidence[0].model_copy(update={"text": f"Leaked credential sk-{'a' * 24}"})
    pack = pack.model_copy(update={"evidence": evidence})
    service = _service(
        tmp_path,
        registry,
        FakeAi(_result(tmp_path, "secret-context")),
        FakeLiveEvidence(pack),
    )

    with pytest.raises(KnowledgeRefreshRejected, match="secret_material"):
        service._context(
            service._pending_jobs(NOW, None),
            (pack,),
            NOW,
            "secret-context",
        )


def test_context_bounds_live_evidence_without_dropping_sources(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    pack = _pack()
    original_texts = {
        pack.evidence[0].source_key: "Normative evidence. " + "n" * 120_000,
        pack.evidence[1].source_key: "Implementation evidence. " + "i" * 120_000,
    }
    evidence = [
        item.model_copy(update={"text": original_texts[item.source_key]}) for item in pack.evidence
    ]
    pack = pack.model_copy(update={"evidence": evidence})
    service = KnowledgeRefresh(
        tmp_path,
        ai=FakeAi(_result(tmp_path, "bounded-context")),
        live_evidence=FakeLiveEvidence(pack),
        registry=registry,
    )

    encoded = service._context(
        service._pending_jobs(NOW, None),
        (pack,),
        NOW,
        "bounded-context",
    )
    context = json.loads(encoded)
    context_items = context["evidence_pack"]["packs"][0]["evidence"]

    assert len(encoded) <= 160_000
    assert {item["source_key"] for item in context_items} == set(original_texts)
    assert all(item["text"] for item in context_items)
    assert all(
        len(item["text"]) < len(original_texts[item["source_key"]]) for item in context_items
    )
    assert all(item["excerpted"] for item in context_items)


def test_pending_jobs_keep_ercs_together_in_a_bounded_batch(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    base_job = KnowledgeRefreshJob.model_validate(
        json.loads((tmp_path / "data/state/pending-knowledge-refresh.json").read_text())[0]
    )
    source_specs = [
        (erc, source.source_key, group)
        for erc, group in ((8004, 0), (8183, 1), (8263, 2))
        for source in registry.resolve(erc, frozenset(EvidenceKind))[: 2 if erc != 8263 else 1]
    ]
    jobs = [
        base_job.model_copy(
            update={
                "job_key": refresh_key(erc, source_key, f"{index:064x}"),
                "erc_number": erc,
                "source_key": source_key,
                "observed_sha256": f"{index:064x}",
                "updated_at": NOW - timedelta(minutes=5 - group),
            }
        )
        for index, (erc, source_key, group) in enumerate(source_specs, start=1)
    ]
    (tmp_path / "data/state/pending-knowledge-refresh.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs]) + "\n",
        encoding="utf-8",
    )
    service = _service(
        tmp_path,
        registry,
        FakeAi(_result(tmp_path, "bounded-batch")),
        FakeLiveEvidence(_pack()),
    )

    pending = service._pending_jobs(NOW, None)

    assert [job.erc_number for job in pending] == [8004, 8004, 8183, 8183]

    constrained = KnowledgeRefresh(
        tmp_path,
        ai=FakeAi(_result(tmp_path, "job-cap-batch")),
        live_evidence=FakeLiveEvidence(_pack()),
        registry=registry,
        max_jobs=3,
    )
    constrained_pending = constrained._pending_jobs(NOW, None)
    assert [job.erc_number for job in constrained_pending] == [8004, 8004]

    uow = RepositoryUnitOfWork(tmp_path, operation_id="resolve-bounded-batch")
    uow.register_external_evidence(())
    constrained.state.stage_compilation_outcome(
        uow,
        (),
        frozenset(job.job_key for job in constrained_pending),
        now=NOW,
    )
    uow.publish()

    assert [job.erc_number for job in constrained.state.load().refresh_jobs] == [
        8183,
        8183,
        8263,
    ]


@pytest.mark.asyncio
async def test_refresh_compiles_live_pack_and_atomically_resolves_job(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    ai = FakeAi(_result(tmp_path, "compile-8004"))
    live = FakeLiveEvidence(_pack())

    result = await _service(tmp_path, registry, ai, live).run(
        cutoff=NOW, operation_id="compile-8004"
    )

    assert result.processed_job_keys == (JOB_KEY,)
    assert result.changed_paths
    assert live.calls[0].erc_numbers == (8004,)
    assert ai.calls[0]["timeout_seconds"] == 900
    context = ai.calls[0]["context_pack"]
    assert CANONICAL_TEXT in context
    assert '"citation_allowlist"' in context
    decoded_context = json.loads(context)
    assert decoded_context["trigger"]["acknowledgement_page_contract"] == {
        "heading": "Related topics",
        "path": "knowledge/acknowledgements/<public-name>.md",
        "purpose": "acknowledge public contributions",
    }
    assert json.loads((tmp_path / "data/state/pending-knowledge-refresh.json").read_text()) == []
    assert CANONICAL_TEXT not in (tmp_path / "knowledge/meta/source-ledger.json").read_text()


@pytest.mark.asyncio
async def test_refresh_binds_controller_owned_result_metadata(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    ai_result = _result(tmp_path, "model-operation")
    ai_result["processed_job_keys"] = ["model-invented-job"]
    ai_result["evidence_gaps"] = []
    ai = FakeAi(ai_result)

    result = await _service(tmp_path, registry, ai, FakeLiveEvidence(_pack())).run(
        cutoff=NOW, operation_id="controller-operation"
    )

    assert result.processed_job_keys == (JOB_KEY,)
    assert json.loads((tmp_path / "data/state/pending-knowledge-refresh.json").read_text()) == []
    assert ai.result["processed_job_keys"] == ["model-invented-job"]
    assert ai.result["evidence_gaps"] == []
    assert ai.result["transaction"]["operation_id"] == "model-operation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack",
    [
        "unknown_source_key",
        "missing_verified_at",
        "missing_observed_version",
        "copied_body",
        "normative_from_implementation",
    ],
)
async def test_refresh_rejects_unbound_or_unverifiable_output(tmp_path: Path, attack: str) -> None:
    registry = _seed(tmp_path)
    result = _result(tmp_path, "compile-8004")
    page_write = result["transaction"]["writes"][0]
    source_write = result["transaction"]["writes"][1]
    claim_write = result["transaction"]["writes"][2]
    if attack == "unknown_source_key":
        page_write["content"] = page_write["content"].replace(
            "erc-8004-canonical", "unknown-source"
        )
        page_write["citations"][0] = "unknown-source"
    elif attack == "missing_verified_at":
        page_write["content"] = page_write["content"].replace(
            "verified_at: 2026-08-23T05:00:00Z\n", ""
        )
    elif attack == "missing_observed_version":
        ledger = json.loads(source_write["content"])
        del ledger["entries"]["erc-8004-canonical"]["observed_version"]
        source_write["content"] = json.dumps(ledger)
    elif attack == "copied_body":
        ledger = json.loads(source_write["content"])
        ledger["entries"]["erc-8004-canonical"]["copied_text"] = CANONICAL_TEXT
        source_write["content"] = json.dumps(ledger)
    else:
        ledger = json.loads(claim_write["content"])
        ledger["entries"]["erc-8004-interface"]["source_keys"] = ["agent-ercs-8004-readme"]
        claim_write["content"] = json.dumps(ledger)

    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for base in (tmp_path / "knowledge", tmp_path / "data/state")
        for path in base.rglob("*")
        if path.is_file()
    }
    with pytest.raises(KnowledgeRefreshRejected):
        await _service(tmp_path, registry, FakeAi(result), FakeLiveEvidence(_pack())).run(
            cutoff=NOW, operation_id="compile-8004"
        )
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for base in (tmp_path / "knowledge", tmp_path / "data/state")
        for path in base.rglob("*")
        if path.is_file()
    }
    assert after == before
