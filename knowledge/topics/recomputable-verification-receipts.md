---
title: "Recomputable Verification Receipts (RVR)"
type: topic
created: "2026-08-28"
updated: "2026-08-28"
source_ids:
- "tg:tawg:3470"
- "tg:tawg:3447"
telegram_record_ids:
- "tg:tawg:3470"
- "tg:tawg:3447"
source_urls:
- "https://github.com/pipavlo82/recomputable-verification-receipts"
- "https://github.com/pipavlo82/recomputable-verification-receipts/releases/tag/v0.0.1-rc.2"
- "https://ethereum-magicians.org/t/recomputable-verification-receipts-rvr/29521"
provenance_status: verified
---

# Recomputable Verification Receipts (RVR)

**RVR** (Recomputable Verification Receipts) is a pre-ERC interoperability proposal. Its goal is to make verification results independently recomputable rather than requiring later reviewers to trust a stored boolean, a signature, or an opaque report digest.

## Status

Pre-ERC — no ERC number has been assigned. An Ethereum Magicians discussion is open at https://ethereum-magicians.org/t/recomputable-verification-receipts-rvr/29521.

## Core Model

RVR separates verification into two orthogonal axes.

**Verification outcome:**
- `VERIFIED`
- `REFUTED`
- `UNVERIFIABLE`

**Recomputation status:**
- `REPRODUCED`
- `DIVERGED`
- `CANNOT_RECOMPUTE`

## Verification Profile

The central object is a content-addressed **Verification Profile** that commits to:

- Verification specification
- Canonical byte contract
- Evidence-set contract
- Canonical-result contract
- Conformance vectors
- Reason namespace
- Every outcome-relevant external-context commitment

### Closure Rule

Every input capable of changing the verification outcome must be included in the committed evidence closure or identified by an immutable commitment/snapshot defined by the Verification Profile.

Querying live chain state, a mutable registry, the current RPC head, a remote API, or the current time is not sufficient for reproducible verification unless the relevant state is immutably committed.

## Experimental Receipt Fields

The experimental receipt carries six fields:

1. `claimDigest`
2. `evidenceSetDigest`
3. `verificationProfileDigest`
4. `outcome`
5. `reasonCode`
6. `resultDigest`

## Explicit Out-of-Scope Items

RVR does **not** define an on-chain registry, proof system, agent identity, reputation, delegation, authentication, settlement, or trusted producer. Other systems may compose with RVR, but they do not become semantic authority over its outcome fields.

## Boundary with ERC-8281 / OCP

ERC-8281 (OCP) establishes commitment and inclusion of exact observation bytes. RVR addresses whether the semantic result derived from those committed inputs can be independently reproduced under the same pinned verification contract and evidence closure. OCP can remain agnostic to inner semantics while composing cleanly with RVR; in RVR, meaning comes from the identified Verification Profile rather than from arbitrary artifact bytes.

## Implementation

- **Current baseline:** https://github.com/pipavlo82/recomputable-verification-receipts
- **Frozen RC2:** https://github.com/pipavlo82/recomputable-verification-receipts/releases/tag/v0.0.1-rc.2

## Sources

- https://github.com/pipavlo82/recomputable-verification-receipts
- https://github.com/pipavlo82/recomputable-verification-receipts/releases/tag/v0.0.1-rc.2
- https://ethereum-magicians.org/t/recomputable-verification-receipts-rvr/29521
