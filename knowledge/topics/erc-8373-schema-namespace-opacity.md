---
title: "ERC-8373 Schema Namespace Opacity"
type: topic
created: "2026-08-31"
updated: "2026-08-31"
source_ids:
- "tg:tawg:3793"
telegram_record_ids:
- "tg:tawg:3793"
source_urls:
- "https://github.com/trustless-ai/recompute-kit/pull/36"
provenance_status: verified
---

# ERC-8373 Schema Namespace Opacity

A normative clarification established in recompute-kit #36 that ERC-8373 enforcers must not branch on the binding-statement schema namespace when deciding admission.

## The Two Version Identifiers

A binding statement under ERC-8373 carries two independent version identifiers governing distinct things:

- **Cutoff profile** (e.g., `pq_key_binding.v1`): the operative side — governs enforcement behavior, cutoff logic, and admission decisions.
- **Binding-statement schema** (implementation-namespaced, e.g., `kya.pq_key_binding.v0` or `invinoveritas.pq_key_binding.v1`): a per-namespace schema version carrying no global ordering across namespaces.

The two identifiers are independent. The `.v0` / `.v1` suffixes in schema names are per-namespace schema versions, not a global ordering. Both `kya.pq_key_binding.v0` and `invinoveritas.pq_key_binding.v1` run under the single shared cutoff profile.

## Normative Rule

An enforcer **MUST NOT** condition its verdict on the schema field. Only the profile governs. The enforcer keys on anchored times, content address, and `pq_pubkey` — never the schema string.

Risk addressed: a third-party enforcer that pattern-matches one namespace would wrongly reject a valid binding issued under a different namespace, even when both operate under the same cutoff profile.

## Failing Witness (Namespace-Opacity Vector)

A conformance vector duplicates the first pre-cutoff case with only the binding's schema swapped to a foreign namespace, asserting an identical ADMIT verdict. The reference enforcer scores 10/10; a mutant that branches on the namespace fails exactly that one case (9/10). This applies the prove-can-fail discipline to the namespace-opacity rule.

## Attribution (Corrected and Owner-Verified)

- **`kya.pq_key_binding.v0`**: the group's own binding (KYA-L4: SLH-DSA, Ethereum-mainnet OCP anchor).
- **`invinoveritas.pq_key_binding.v1`**: Fede's first live binding, hosted at `api.babyblueviper.com`. Fede independently recomputed the recompute-kit #36 vector note against his own live `.well-known` endpoint and confirmed byte-match.

Attribution was previously mislabeled and has been corrected and owner-verified throughout all relevant artifacts.

## ERC PR Status (ethereum/ERCs #1932)

Author line complete: Tiago, Fede (@babyblueviper1), Pavlo (@pipavlo82), Matthias Hauser (@0x2kNJ), Faisal Firdani (@zexoverz). Both new authors confirmed and all CI green. Only remaining gate is EIP-editor review.

## Next Steps (Gated on recompute-kit #36 Merge)

1. Mirror the MUST-NOT rule and namespace-opacity vector into the ERC's own `assets/erc-8373/` copy (rides #1932, re-triggers eip-bot).
2. Repin the live gateway `/pq/enforce/selftest` to the merge commit.

## Sources

- https://github.com/trustless-ai/recompute-kit/pull/36
