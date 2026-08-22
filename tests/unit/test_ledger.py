from datetime import UTC, datetime, timedelta

import pytest

from tawg_bot.ledger import (
    ClaimAssessment,
    ClaimRisk,
    ClaimState,
    EvidenceLedger,
    InsufficientEvidence,
    SourceAuthority,
    SourceEvidence,
)

NOW = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)


def evidence(
    source_id: str,
    *,
    authority: SourceAuthority = SourceAuthority.PRIMARY,
    independence_key: str | None = None,
    active: bool = True,
    fresh: bool = True,
) -> SourceEvidence:
    return SourceEvidence(
        source_id=source_id,
        authority=authority,
        independence_key=independence_key or source_id,
        active=active,
        fresh_until=NOW + timedelta(days=1) if fresh else NOW - timedelta(seconds=1),
    )


def test_ordinary_accepted_claim_requires_one_fresh_active_non_synthetic_source() -> None:
    ledger = EvidenceLedger([evidence("tg:tawg:1")])
    claim = ClaimAssessment(
        claim_id="claim:ordinary",
        state=ClaimState.ACCEPTED,
        risk=ClaimRisk.ORDINARY,
        source_ids=["tg:tawg:1"],
        assessed_at=NOW,
    )

    ledger.validate_claim(claim)

    for bad_source in (
        evidence("synthetic:1", authority=SourceAuthority.SYNTHETIC),
        evidence("inactive:1", active=False),
        evidence("stale:1", fresh=False),
    ):
        bad_claim = claim.model_copy(update={"source_ids": [bad_source.source_id]})
        with pytest.raises(InsufficientEvidence):
            EvidenceLedger([bad_source]).validate_claim(bad_claim)


def test_high_risk_accepted_claim_requires_two_independent_sources() -> None:
    same_origin = [
        evidence("gh:repo:issue:1", independence_key="proposal"),
        evidence("gh:repo:comment:2", independence_key="proposal"),
    ]
    claim = ClaimAssessment(
        claim_id="claim:settlement",
        state=ClaimState.ACCEPTED,
        risk=ClaimRisk.HIGH,
        source_ids=[item.source_id for item in same_origin],
        assessed_at=NOW,
    )

    with pytest.raises(InsufficientEvidence):
        EvidenceLedger(same_origin).validate_claim(claim)

    independent = [
        *same_origin,
        evidence("magicians:27902:post:1", independence_key="forum"),
    ]
    accepted = claim.model_copy(
        update={"source_ids": [item.source_id for item in independent]}
    )
    EvidenceLedger(independent).validate_claim(accepted)


def test_provisional_claim_may_record_a_gap_but_unknown_sources_are_rejected() -> None:
    ledger = EvidenceLedger([])
    provisional = ClaimAssessment(
        claim_id="claim:gap",
        state=ClaimState.PROVISIONAL,
        risk=ClaimRisk.ORDINARY,
        source_ids=[],
        assessed_at=NOW,
    )
    ledger.validate_claim(provisional)

    with pytest.raises(KeyError):
        ledger.validate_claim(
            provisional.model_copy(update={"source_ids": ["missing:source"]})
        )
