"""Resolve, classify, and package current ERC evidence for one AI operation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator

from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.evidence_fetch import (
    EvidenceFetchRejected,
    FetchBudget,
    FetchedEvidence,
)
from tawg_bot.models import StrictModel
from tawg_bot.source_registry import (
    EvidenceAuthority,
    EvidenceKind,
    RegisteredSource,
    SourceRegistry,
)
from tawg_bot.vault import parse_frontmatter


class EvidenceFetcher(Protocol):
    async def fetch(
        self,
        source: RegisteredSource,
        *,
        now: datetime,
        budget: FetchBudget,
    ) -> FetchedEvidence: ...


class OrientationChunk(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    path: str
    verified_at: datetime | None
    authoritative: Literal[False] = False
    text: str

    @field_validator("verified_at")
    @classmethod
    def verified_at_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        _require_utc(value, "orientation verification time")
        return value.astimezone(UTC)


class EvidenceItem(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    source_key: str
    kind: EvidenceKind
    authority: EvidenceAuthority
    canonical_url: str
    citation_url: str
    observed_at: datetime
    version: str | None
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_byte_count: int = Field(ge=0)
    excerpted: bool
    untrusted_evidence: Literal[True] = True
    text: str

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "evidence observation time")
        return value.astimezone(UTC)


class MissingEvidence(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    intent: ErcIntent
    bucket: str
    source_key: str | None
    safe_error_code: str


class SourceChange(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    source_key: str
    previous_version: str | None
    observed_version: str | None
    previous_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    observed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def change_time_is_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "source change time")
        return value.astimezone(UTC)


class EvidenceObservation(StrictModel):
    """Body-free evidence metadata allowed to cross into repository state code."""

    source_key: str
    observed_at: datetime
    version: str | None
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_byte_count: int = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def observation_time_is_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "evidence metadata observation time")
        return value.astimezone(UTC)


class EvidencePersistenceOutcome(StrictModel):
    """The complete body-free projection consumed by persistent state writers."""

    query: ErcQuery
    observations: list[EvidenceObservation]
    missing_required: list[MissingEvidence]
    source_changes: list[SourceChange]


class EvidencePack(StrictModel):
    schema_version: Literal["tawg.evidence-pack.v1"] = "tawg.evidence-pack.v1"
    query: ErcQuery
    generated_orientation: list[OrientationChunk]
    evidence: list[EvidenceItem]
    citation_allowlist: list[str]
    missing_required: list[MissingEvidence]
    source_changes: list[SourceChange]

    def for_persistence(self) -> EvidencePersistenceOutcome:
        return EvidencePersistenceOutcome(
            query=self.query,
            observations=[
                EvidenceObservation(
                    source_key=item.source_key,
                    observed_at=item.observed_at,
                    version=item.version,
                    content_sha256=item.content_sha256,
                    source_byte_count=item.source_byte_count,
                )
                for item in self.evidence
            ],
            missing_required=self.missing_required,
            source_changes=self.source_changes,
        )


_KIND_RANK = {
    EvidenceKind.NORMATIVE_SPEC: 0,
    EvidenceKind.IMPLEMENTATION: 1,
    EvidenceKind.TEST: 2,
    EvidenceKind.EXAMPLE: 2,
    EvidenceKind.DISCUSSION: 3,
}
_AUTHORITY_RANK = {
    EvidenceAuthority.CANONICAL: 0,
    EvidenceAuthority.OFFICIAL_ORG: 1,
    EvidenceAuthority.MAINTAINER: 2,
    EvidenceAuthority.COMMUNITY: 3,
}
_REQUIRED: dict[ErcIntent, tuple[tuple[str, frozenset[EvidenceKind]], ...]] = {
    ErcIntent.OVERVIEW: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.IMPLEMENTATION: (
        ("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),
        ("implementation", frozenset({EvidenceKind.IMPLEMENTATION})),
        ("test_or_example", frozenset({EvidenceKind.TEST, EvidenceKind.EXAMPLE})),
    ),
    ErcIntent.INTERFACES: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.STATE_MACHINE: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.SECURITY: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.STATUS: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.COMPARISON: (("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),),
    ErcIntent.DISCUSSION: (
        ("normative_spec", frozenset({EvidenceKind.NORMATIVE_SPEC})),
        ("discussion", frozenset({EvidenceKind.DISCUSSION})),
    ),
}


class LiveEvidenceService:
    def __init__(
        self,
        *,
        root: Path,
        registry: SourceRegistry,
        fetcher: EvidenceFetcher,
        max_sources: int = 32,
        max_total_bytes: int = 4_000_000,
        operation_seconds: float = 60.0,
        max_evidence_chars: int = 80_000,
        max_orientation_chars: int = 20_000,
    ) -> None:
        if max_sources <= 0 or max_total_bytes <= 0 or operation_seconds <= 0:
            raise ValueError("live evidence budgets must be positive")
        if max_evidence_chars < 1000 or max_orientation_chars < 1000:
            raise ValueError("live evidence text budgets must be at least 1000 characters")
        self.root = root.resolve()
        self.registry = registry
        self.fetcher = fetcher
        self.max_sources = max_sources
        self.max_total_bytes = max_total_bytes
        self.operation_seconds = operation_seconds
        self.max_evidence_chars = max_evidence_chars
        self.max_orientation_chars = max_orientation_chars

    async def build(self, query: ErcQuery, *, now: datetime) -> EvidencePack:
        _require_utc(now, "live evidence build time")
        budget = FetchBudget(
            max_sources=self.max_sources,
            max_total_bytes=self.max_total_bytes,
            deadline=now + timedelta(seconds=self.operation_seconds),
        )
        items: list[EvidenceItem] = []
        changes: list[SourceChange] = []
        failures: dict[str, str] = {}
        sources_by_erc: dict[int, tuple[RegisteredSource, ...]] = {}
        for erc_number in query.erc_numbers:
            sources = self.registry.resolve(erc_number, frozenset(EvidenceKind))
            sources_by_erc[erc_number] = sources
            for source in sources:
                try:
                    fetched = await self.fetcher.fetch(source, now=now, budget=budget)
                except EvidenceFetchRejected as error:
                    failures[source.source_key] = error.code
                    continue
                text = fetched.text[: self.max_evidence_chars]
                items.append(
                    EvidenceItem(
                        erc_number=erc_number,
                        source_key=source.source_key,
                        kind=source.kind,
                        authority=source.authority,
                        canonical_url=fetched.canonical_url,
                        citation_url=fetched.citation_url,
                        observed_at=fetched.observed_at,
                        version=fetched.version,
                        content_sha256=fetched.content_sha256,
                        source_byte_count=fetched.byte_count,
                        excerpted=len(fetched.text) > len(text),
                        text=text,
                    )
                )
                observed = source.last_observed
                if observed is None or (
                    observed.content_sha256 != fetched.content_sha256
                    or observed.version != fetched.version
                ):
                    changes.append(
                        SourceChange(
                            erc_number=erc_number,
                            source_key=source.source_key,
                            previous_version=observed.version if observed else None,
                            observed_version=fetched.version,
                            previous_sha256=observed.content_sha256 if observed else None,
                            observed_sha256=fetched.content_sha256,
                            observed_at=now,
                        )
                    )

        erc_rank = {number: index for index, number in enumerate(query.erc_numbers)}
        items.sort(
            key=lambda item: (
                _KIND_RANK[item.kind],
                erc_rank[item.erc_number],
                _AUTHORITY_RANK[item.authority],
                item.source_key,
            )
        )
        changes.sort(key=lambda change: (erc_rank[change.erc_number], change.source_key))
        missing = self._missing(query, items, sources_by_erc, failures)
        citations = list(dict.fromkeys(item.citation_url for item in items))
        return EvidencePack(
            query=query,
            generated_orientation=self._orientation(query),
            evidence=items,
            citation_allowlist=citations,
            missing_required=missing,
            source_changes=changes,
        )

    def _missing(
        self,
        query: ErcQuery,
        items: list[EvidenceItem],
        sources_by_erc: dict[int, tuple[RegisteredSource, ...]],
        failures: dict[str, str],
    ) -> list[MissingEvidence]:
        missing: list[MissingEvidence] = []
        for erc_number in query.erc_numbers:
            fetched_kinds = {item.kind for item in items if item.erc_number == erc_number}
            registered = sources_by_erc[erc_number]
            for bucket, alternatives in _REQUIRED[query.intent]:
                if fetched_kinds.intersection(alternatives):
                    continue
                candidates = [source for source in registered if source.kind in alternatives]
                source_key = candidates[0].source_key if candidates else None
                code = (
                    failures.get(source_key, "unregistered_source")
                    if source_key is not None
                    else "unregistered_source"
                )
                missing.append(
                    MissingEvidence(
                        erc_number=erc_number,
                        intent=query.intent,
                        bucket=bucket,
                        source_key=source_key,
                        safe_error_code=code,
                    )
                )
        return missing

    def _orientation(self, query: ErcQuery) -> list[OrientationChunk]:
        chunks: list[OrientationChunk] = []
        for erc_number in query.erc_numbers:
            relative = f"knowledge/ercs/erc-{erc_number}.md"
            path = self.root / relative
            if not path.is_file() or path.is_symlink():
                continue
            frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            verified_at = _frontmatter_time(frontmatter.get("verified_at") if frontmatter else None)
            chunks.append(
                OrientationChunk(
                    erc_number=erc_number,
                    path=relative,
                    verified_at=verified_at,
                    text=body[: self.max_orientation_chars],
                )
            )
        return chunks


def _frontmatter_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    try:
        _require_utc(parsed, "orientation verification time")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
