from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.evidence_fetch import EvidenceFetchRejected, FetchBudget, FetchedEvidence
from tawg_bot.live_evidence import LiveEvidenceService
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.source_registry import EvidenceKind, RegisteredSource, SourceRegistry
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 23, 5, tzinfo=UTC)
EXTERNAL_RECORD_ID = re.compile(
    r"(?<![A-Za-z0-9_-])(?:gh:[A-Za-z0-9_.-]+:(?:file|commit|pr|issue)|magicians:\d+:post:)"
)
BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "excerpt",
        "html",
        "markdown",
        "raw",
        "source_records",
        "text",
        "text_original",
    }
)
METADATA_PATHS = (
    "knowledge/meta/sources.yml",
    "knowledge/meta/source-ledger.json",
    "knowledge/meta/claim-ledger.json",
    "data/state/knowledge-gaps.json",
    "data/state/pending-knowledge-refresh.json",
    "data/state/source-cursors.json",
)


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {
            *(str(key) for key in value),
            *(nested for child in value.values() for nested in _walk_keys(child)),
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return {nested for child in value for nested in _walk_keys(child)}
    return set()


def _load(relative: str) -> Any:
    path = ROOT / relative
    if path.suffix in {".yml", ".yaml"}:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_repository_persists_telegram_and_metadata_not_external_bodies() -> None:
    assert not (ROOT / "data/github").exists()
    assert not (ROOT / "data/magicians").exists()
    assert not (ROOT / "data/state/magicians-candidates.json").exists()

    telegram_files = sorted((ROOT / "data/telegram").rglob("*.jsonl"))
    assert telegram_files
    assert sum(path.stat().st_size for path in telegram_files) > 0

    for relative in METADATA_PATHS:
        assert not (_walk_keys(_load(relative)) & BODY_KEYS), relative


def test_generated_knowledge_uses_no_legacy_external_record_ids() -> None:
    for path in sorted((ROOT / "knowledge").rglob("*")):
        if path.is_file():
            assert not EXTERNAL_RECORD_ID.search(path.read_text(encoding="utf-8")), path


def test_generated_knowledge_links_and_v2_ledgers_validate() -> None:
    source_ledger = _load("knowledge/meta/source-ledger.json")
    claim_ledger = _load("knowledge/meta/claim-ledger.json")
    assert source_ledger["schema"] == "tawg.source-ledger.v2"
    assert claim_ledger["schema"] == "tawg.claim-ledger.v2"

    report = VaultLinter(ROOT).lint()
    assert report.error_count == 0, report.findings


def test_publish_boundary_rejects_live_external_body_in_state(tmp_path: Path) -> None:
    external_body = (
        "MAGICIANS-CANARY-b17d: this discussion body is transient evidence and cannot be "
        "stored in any repository state artifact after the operation completes."
    )
    uow = RepositoryUnitOfWork(tmp_path, operation_id="live-boundary")
    uow.register_external_evidence((external_body,))

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        uow.stage_json(
            "data/state/pending-knowledge-refresh.json",
            [{"safe_error_code": external_body}],
        )

    assert not (tmp_path / "data/state/pending-knowledge-refresh.json").exists()


class _CurrentFetcher:
    def __init__(self, failures: frozenset[str] = frozenset()) -> None:
        self.failures = failures

    async def fetch(
        self, source: RegisteredSource, *, now: datetime, budget: FetchBudget
    ) -> FetchedEvidence:
        if source.source_key in self.failures:
            raise EvidenceFetchRejected("timeout")
        text = f"Current acceptance evidence for {source.source_key}."
        return FetchedEvidence(
            source_key=source.source_key,
            canonical_url=source.canonical_url,
            citation_url=source.immutable_url or source.canonical_url,
            media_type="text/plain",
            observed_at=now,
            version=f"acceptance-{source.source_key}",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            byte_count=len(text.encode()),
            text=text,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("erc_number", [8004, 8183])
@pytest.mark.parametrize(
    "intent",
    [
        ErcIntent.OVERVIEW,
        ErcIntent.INTERFACES,
        ErcIntent.STATE_MACHINE,
        ErcIntent.IMPLEMENTATION,
        ErcIntent.SECURITY,
        ErcIntent.DISCUSSION,
    ],
)
async def test_erc_acceptance_uses_current_ranked_evidence_for_each_question_shape(
    tmp_path: Path, erc_number: int, intent: ErcIntent
) -> None:
    registry = SourceRegistry.from_yaml(ROOT / "knowledge/meta/sources.yml")
    pack = await LiveEvidenceService(
        root=tmp_path,
        registry=registry,
        fetcher=_CurrentFetcher(),
    ).build(ErcQuery(erc_numbers=(erc_number,), intent=intent), now=NOW)

    assert pack.evidence[0].kind is EvidenceKind.NORMATIVE_SPEC
    assert pack.citation_allowlist == list(
        dict.fromkeys(item.citation_url for item in pack.evidence)
    )
    assert all(item.observed_at == NOW for item in pack.evidence)
    assert all(item.untrusted_evidence for item in pack.evidence)
    assert pack.source_changes
    if intent is ErcIntent.DISCUSSION:
        assert any(item.kind is EvidenceKind.DISCUSSION for item in pack.evidence)
    if intent is ErcIntent.IMPLEMENTATION:
        assert any(gap.bucket == "test_or_example" for gap in pack.missing_required)
        if erc_number == 8183:
            assert any(gap.bucket == "implementation" for gap in pack.missing_required)


@pytest.mark.asyncio
async def test_unreachable_normative_source_is_a_gap_and_never_citable(tmp_path: Path) -> None:
    registry = SourceRegistry.from_yaml(ROOT / "knowledge/meta/sources.yml")
    canonical = registry.resolve(8183, frozenset({EvidenceKind.NORMATIVE_SPEC}))[0]
    pack = await LiveEvidenceService(
        root=tmp_path,
        registry=registry,
        fetcher=_CurrentFetcher(frozenset({canonical.source_key})),
    ).build(ErcQuery(erc_numbers=(8183,), intent=ErcIntent.OVERVIEW), now=NOW)

    assert canonical.canonical_url not in pack.citation_allowlist
    assert "https://fabricated.example/erc-8183" not in pack.citation_allowlist
    assert [(gap.bucket, gap.source_key, gap.safe_error_code) for gap in pack.missing_required] == [
        ("normative_spec", canonical.source_key, "timeout")
    ]
