"""Explicit source authority and claim-assessment rules."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator

from tawg_bot.models import StrictModel
from tawg_bot.source_registry import EvidenceAuthority, EvidenceKind


class SourceAuthority(StrEnum):
    OFFICIAL = "official"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    COMMUNITY = "community"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class ClaimState(StrEnum):
    ACCEPTED = "accepted"
    PROVISIONAL = "provisional"
    CONTESTED = "contested"
    UNSUPPORTED = "unsupported"
    DEPRECATED = "deprecated"


class ClaimRisk(StrEnum):
    ORDINARY = "ordinary"
    HIGH = "high"


class ClaimKind(StrEnum):
    ORDINARY = "ordinary"
    NORMATIVE = "normative"
    IMPLEMENTATION = "implementation"
    STATUS = "status"
    DISCUSSION = "discussion"


class InsufficientEvidence(ValueError):
    """Raised when a requested claim state exceeds its active evidence."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("ledger timestamps must use UTC")
    return value.astimezone(UTC)


class SourceEvidence(StrictModel):
    source_id: str = Field(min_length=1, max_length=256)
    authority: SourceAuthority
    independence_key: str = Field(min_length=1, max_length=256)
    active: bool = True
    fresh_until: datetime

    _fresh_until_utc = field_validator("fresh_until")(_utc)


class ClaimAssessment(StrictModel):
    claim_id: str = Field(min_length=1, max_length=256)
    state: ClaimState
    risk: ClaimRisk
    source_ids: list[str]
    assessed_at: datetime

    _assessed_at_utc = field_validator("assessed_at")(_utc)


class SourceEvidenceV2(StrictModel):
    source_key: str = Field(min_length=1, max_length=128)
    source_kind: EvidenceKind
    authority: EvidenceAuthority
    canonical_url: str = Field(min_length=1, max_length=2048)
    observed_version: str | None
    observed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime
    independence_key: str = Field(min_length=1, max_length=256)
    active: bool = True

    _observed_at_utc = field_validator("observed_at")(_utc)


class ClaimAssessmentV2(StrictModel):
    claim_id: str = Field(min_length=1, max_length=256)
    claim_kind: ClaimKind
    state: ClaimState
    risk: ClaimRisk
    source_keys: list[str]
    assessed_at: datetime

    _assessed_at_utc = field_validator("assessed_at")(_utc)


class EvidenceLedger:
    _CREDIBLE = frozenset(
        {
            SourceAuthority.OFFICIAL,
            SourceAuthority.PRIMARY,
            SourceAuthority.SECONDARY,
            SourceAuthority.COMMUNITY,
        }
    )

    def __init__(self, sources: Iterable[SourceEvidence | SourceEvidenceV2]) -> None:
        self.sources: dict[str, SourceEvidence | SourceEvidenceV2] = {}
        for source in sources:
            source_id = (
                source.source_id if isinstance(source, SourceEvidence) else source.source_key
            )
            if source_id in self.sources:
                raise ValueError(f"duplicate source evidence: {source_id}")
            self.sources[source_id] = source

    @classmethod
    def from_file(cls, path: Path) -> EvidenceLedger:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") not in {
            "tawg.source-ledger.v1",
            "tawg.source-ledger.v2",
        }:
            raise ValueError("invalid source ledger")
        if not isinstance(raw.get("entries"), dict):
            raise ValueError("invalid source ledger")
        return cls.from_entries(raw["entries"], schema=str(raw["schema"]))

    @classmethod
    def from_entries(
        cls, entries: dict[str, object], *, schema: str = "tawg.source-ledger.v1"
    ) -> EvidenceLedger:
        if schema not in {"tawg.source-ledger.v1", "tawg.source-ledger.v2"}:
            raise ValueError("invalid source ledger")
        sources: list[SourceEvidence | SourceEvidenceV2] = []
        for source_id, value in entries.items():
            if not isinstance(value, dict):
                raise ValueError("invalid source ledger entry")
            id_field = "source_id" if schema.endswith(".v1") else "source_key"
            embedded_id = value.get(id_field)
            if embedded_id is not None and embedded_id != source_id:
                raise ValueError(f"source ledger key does not match {id_field}")
            payload = {id_field: source_id, **value}
            model = SourceEvidence if schema.endswith(".v1") else SourceEvidenceV2
            sources.append(model.model_validate(payload))
        return cls(sources)

    def validate_claim(self, claim: ClaimAssessment | ClaimAssessmentV2) -> None:
        source_ids = claim.source_ids if isinstance(claim, ClaimAssessment) else claim.source_keys
        try:
            referenced = [self.sources[source_id] for source_id in source_ids]
        except KeyError as error:
            raise KeyError(f"claim references unknown source: {error.args[0]}") from None
        if claim.state is not ClaimState.ACCEPTED:
            return
        if isinstance(claim, ClaimAssessmentV2):
            eligible = [source for source in referenced if self._eligible_v2(source, claim)]
        else:
            eligible = [
                source
                for source in referenced
                if isinstance(source, SourceEvidence)
                and source.active
                and source.fresh_until >= claim.assessed_at
                and source.authority in self._CREDIBLE
            ]
        required = 2 if claim.risk is ClaimRisk.HIGH else 1
        independent = {source.independence_key for source in eligible}
        if len(independent) < required:
            raise InsufficientEvidence(
                f"accepted {claim.risk.value} claim requires {required} independent active sources"
            )

    @staticmethod
    def _eligible_v2(source: SourceEvidence | SourceEvidenceV2, claim: ClaimAssessmentV2) -> bool:
        if not isinstance(source, SourceEvidenceV2) or not source.active:
            return False
        if claim.claim_kind is ClaimKind.NORMATIVE:
            return source.source_kind is EvidenceKind.NORMATIVE_SPEC
        if claim.claim_kind is ClaimKind.IMPLEMENTATION:
            return source.source_kind in {
                EvidenceKind.IMPLEMENTATION,
                EvidenceKind.TEST,
                EvidenceKind.EXAMPLE,
            }
        if claim.claim_kind is ClaimKind.DISCUSSION:
            return source.source_kind is EvidenceKind.DISCUSSION
        return True
