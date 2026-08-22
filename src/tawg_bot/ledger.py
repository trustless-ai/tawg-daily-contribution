"""Explicit source authority and claim-assessment rules."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator

from tawg_bot.models import StrictModel


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


class EvidenceLedger:
    _CREDIBLE = frozenset(
        {
            SourceAuthority.OFFICIAL,
            SourceAuthority.PRIMARY,
            SourceAuthority.SECONDARY,
            SourceAuthority.COMMUNITY,
        }
    )

    def __init__(self, sources: Iterable[SourceEvidence]) -> None:
        self.sources: dict[str, SourceEvidence] = {}
        for source in sources:
            if source.source_id in self.sources:
                raise ValueError(f"duplicate source evidence: {source.source_id}")
            self.sources[source.source_id] = source

    @classmethod
    def from_file(cls, path: Path) -> EvidenceLedger:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != "tawg.source-ledger.v1":
            raise ValueError("invalid source ledger")
        if not isinstance(
            raw.get("entries"), dict
        ):
            raise ValueError("invalid source ledger")
        return cls.from_entries(raw["entries"])

    @classmethod
    def from_entries(cls, entries: dict[str, object]) -> EvidenceLedger:
        sources: list[SourceEvidence] = []
        for source_id, value in entries.items():
            if not isinstance(value, dict):
                raise ValueError("invalid source ledger entry")
            embedded_id = value.get("source_id")
            if embedded_id is not None and embedded_id != source_id:
                raise ValueError("source ledger key does not match source_id")
            payload = {"source_id": source_id, **value}
            sources.append(SourceEvidence.model_validate(payload))
        return cls(sources)

    def validate_claim(self, claim: ClaimAssessment) -> None:
        try:
            referenced = [self.sources[source_id] for source_id in claim.source_ids]
        except KeyError as error:
            raise KeyError(f"claim references unknown source: {error.args[0]}") from None
        if claim.state is not ClaimState.ACCEPTED:
            return
        eligible = [
            source
            for source in referenced
            if source.active
            and source.fresh_until >= claim.assessed_at
            and source.authority in self._CREDIBLE
        ]
        required = 2 if claim.risk is ClaimRisk.HIGH else 1
        independent = {source.independence_key for source in eligible}
        if len(independent) < required:
            raise InsufficientEvidence(
                f"accepted {claim.risk.value} claim requires {required} independent active sources"
            )
