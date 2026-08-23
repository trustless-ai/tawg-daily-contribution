from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.knowledge_jobs import KnowledgeStateRejected, KnowledgeStateStore
from tawg_bot.live_evidence import (
    EvidenceItem,
    EvidencePack,
    MissingEvidence,
    SourceChange,
)
from tawg_bot.source_registry import EvidenceAuthority, EvidenceKind, SourceRegistry
from tawg_bot.unit_of_work import RepositoryUnitOfWork

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)


def _seed(root: Path) -> SourceRegistry:
    meta = root / "knowledge/meta"
    meta.mkdir(parents=True)
    registry_path = meta / "sources.yml"
    registry_path.write_bytes((ROOT / "knowledge/meta/sources.yml").read_bytes())
    state = root / "data/state"
    state.mkdir(parents=True)
    for name in (
        "knowledge-gaps.json",
        "pending-knowledge-refresh.json",
        "source-candidates.json",
    ):
        (state / name).write_text("[]\n", encoding="utf-8")
    return SourceRegistry.from_yaml(registry_path)


def _pack(
    *,
    observed_hash: str = "b" * 64,
    missing: bool = True,
    text: str = "EXTERNAL-BODY-CANARY",
) -> EvidencePack:
    item = EvidenceItem(
        erc_number=8004,
        source_key="erc-8004-canonical",
        kind=EvidenceKind.NORMATIVE_SPEC,
        authority=EvidenceAuthority.CANONICAL,
        canonical_url="https://eips.ethereum.org/EIPS/eip-8004",
        citation_url="https://eips.ethereum.org/EIPS/eip-8004",
        observed_at=NOW,
        version="v2",
        content_sha256=observed_hash,
        source_byte_count=len(text.encode()),
        excerpted=False,
        text=text,
    )
    gaps = (
        [
            MissingEvidence(
                erc_number=8004,
                intent=ErcIntent.IMPLEMENTATION,
                bucket="test_or_example",
                source_key=None,
                safe_error_code="unregistered_source",
            )
        ]
        if missing
        else []
    )
    return EvidencePack(
        query=ErcQuery(erc_numbers=(8004,), intent=ErcIntent.IMPLEMENTATION),
        generated_orientation=[],
        evidence=[item],
        citation_allowlist=[item.citation_url],
        missing_required=gaps,
        source_changes=[
            SourceChange(
                erc_number=8004,
                source_key=item.source_key,
                previous_version="v1",
                observed_version="v2",
                previous_sha256="a" * 64,
                observed_sha256=observed_hash,
                observed_at=NOW,
            )
        ],
    )


def _apply(root: Path, store: KnowledgeStateStore, pack: EvidencePack, now: datetime) -> None:
    uow = RepositoryUnitOfWork(root, operation_id=f"evidence-{int(now.timestamp())}")
    uow.register_external_evidence(item.text for item in pack.evidence)
    store.stage_evidence_outcome(uow, pack.for_persistence(), now=now)
    uow.publish()


def test_state_store_deduplicates_current_gaps_and_refresh_jobs(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    _apply(tmp_path, KnowledgeStateStore(tmp_path, registry=registry), _pack(), NOW)

    first = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()
    _apply(
        tmp_path,
        KnowledgeStateStore(
            tmp_path,
            registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
        ),
        _pack(),
        NOW + timedelta(minutes=1),
    )
    second = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()

    assert len(second.gaps) == 1
    assert second.gaps[0].first_observed_at == first.gaps[0].first_observed_at
    assert second.gaps[0].last_observed_at == NOW + timedelta(minutes=1)
    assert len(second.refresh_jobs) == 1
    assert second.refresh_jobs[0].observed_sha256 == "b" * 64
    assert second.refresh_jobs[0].created_at == first.refresh_jobs[0].created_at
    assert second.refresh_jobs[0].updated_at == NOW + timedelta(minutes=1)


def test_new_hash_supersedes_old_pending_refresh_for_same_source(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    _apply(tmp_path, KnowledgeStateStore(tmp_path, registry=registry), _pack(), NOW)
    updated_hash = "c" * 64
    _apply(
        tmp_path,
        KnowledgeStateStore(
            tmp_path,
            registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
        ),
        _pack(observed_hash=updated_hash),
        NOW + timedelta(minutes=1),
    )

    state = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()

    assert len(state.refresh_jobs) == 1
    assert state.refresh_jobs[0].observed_sha256 == updated_hash


def test_successful_bucket_removes_the_current_gap(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    _apply(tmp_path, KnowledgeStateStore(tmp_path, registry=registry), _pack(), NOW)
    _apply(
        tmp_path,
        KnowledgeStateStore(
            tmp_path,
            registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
        ),
        _pack(missing=False),
        NOW + timedelta(minutes=1),
    )

    state = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    ).load()

    assert state.gaps == ()


def test_resolve_refresh_removes_only_the_completed_job(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    _apply(tmp_path, KnowledgeStateStore(tmp_path, registry=registry), _pack(), NOW)
    store = KnowledgeStateStore(
        tmp_path,
        registry=SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml"),
    )
    job_key = store.load().refresh_jobs[0].job_key
    uow = RepositoryUnitOfWork(tmp_path, operation_id="resolve-refresh")
    uow.register_external_evidence(())

    store.resolve_refresh(uow, job_key)
    uow.publish()

    assert store.load().refresh_jobs == ()
    assert len(store.load().gaps) == 1


def test_state_and_registry_never_serialize_transient_evidence_text(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    _apply(tmp_path, KnowledgeStateStore(tmp_path, registry=registry), _pack(), NOW)

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((tmp_path / "data/state").glob("*.json"))
    ) + (tmp_path / "knowledge/meta/sources.yml").read_text(encoding="utf-8")

    assert "EXTERNAL-BODY-CANARY" not in persisted
    observed = (
        SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml")
        .source("erc-8004-canonical")
        .last_observed
    )
    assert observed is not None
    assert observed.content_sha256 == "b" * 64


def test_candidate_is_normalized_and_stored_without_registry_promotion(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    store = KnowledgeStateStore(tmp_path, registry=registry)
    uow = RepositoryUnitOfWork(tmp_path, operation_id="candidate-one")
    uow.register_external_evidence(())

    store.add_candidate(
        uow,
        "https://Ethereum-Magicians.org/t/proposal/123#latest",
        "tg:tawg:50",
        NOW,
    )
    uow.publish()

    state = KnowledgeStateStore(tmp_path, registry=registry).load()
    assert len(state.candidates) == 1
    assert state.candidates[0].url == "https://ethereum-magicians.org/t/proposal/123"
    with pytest.raises(KeyError):
        registry.source(state.candidates[0].candidate_key)


@pytest.mark.parametrize(
    "url",
    [
        "http://ethereum-magicians.org/t/123",
        "https://user:pass@ethereum-magicians.org/t/123",
        "https://ethereum-magicians.org/t/123?token=secret",
        "https://127.0.0.1/admin",
    ],
)
def test_candidate_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    store = KnowledgeStateStore(tmp_path, registry=_seed(tmp_path))
    uow = RepositoryUnitOfWork(tmp_path, operation_id="candidate-rejected")
    uow.register_external_evidence(())

    with pytest.raises(KnowledgeStateRejected, match="candidate_url"):
        store.add_candidate(uow, url, "tg:tawg:50", NOW)


def test_state_loader_rejects_body_shaped_or_invalid_entries(tmp_path: Path) -> None:
    registry = _seed(tmp_path)
    (tmp_path / "data/state/knowledge-gaps.json").write_text(
        json.dumps([{"body": "external"}]), encoding="utf-8"
    )

    with pytest.raises(KnowledgeStateRejected, match="invalid knowledge state"):
        KnowledgeStateStore(tmp_path, registry=registry).load()
