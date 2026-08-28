---
title: "Canonical Serializer Split"
type: topic
created: "2026-08-28"
updated: "2026-08-28"
source_ids:
- "tg:tawg:3511"
- "tg:tawg:3559"
telegram_record_ids:
- "tg:tawg:3511"
- "tg:tawg:3559"
provenance_status: verified
---

# Canonical Serializer Split

Status of the canonical-serializer split, as recorded by Merlini.

## Two Serializers, Two Domains

The split is deliberate — two serializers serve two distinct artifact families:

### encode-json-utf8-lf.v0

- UTF-16 code-unit ordering, negative-zero rejection, LF line endings.
- Landed as a frozen contract in `recompute-kit/conformance/encode-json-utf8-lf-v0` (PR \#21, merge `140874db`).
- Spec hash: `22207f8c4047…`, vectors hash: `8d53ab1d3dfb…`.
- TSEI's frozen-artifact serializer.
- Pavlo's producer adoption: `crystal-receipt#218` (producer-side only; recompute confirmed at head `933a8061`).

### RFC-8785 JCS (Unicode scalar-value ordering)

- Used by: erc-8309 envelope §5 (`x-canonical-serializer`), `receiptos-c14n-v0`, `crc_claim`, `verify/canon`.
- Not changing.

## Why Our Side Is Confirmation, Not Migration

The erc-8309 envelope conformance carries a wrong-serializer counterfactual leg that discharges against `encode-json-utf8-lf.v0`. It recomputes the envelope's `encodeJsonUtf8Lf` digest and requires it to DISTINCT-differ from the JCS digest (3 ENCODED/DISTINCT + 1 REJECTED).

Migrating any JCS producers to `.v0` would break a control that exists precisely to prove they are different contracts. JCS producers stay JCS — that is the ratified answer for them.

## Open Items

1. **Formalize the split as a normative mapping** — a durable statement of "which artifact family uses which serializer" (8309 envelope + receipts → JCS; TSEI frozen artifacts → `encode-json-utf8-lf.v0`), so a cold reader does not have to infer it from the counterfactual leg. This is the open half of the "§5 canonical-form split" item.

2. **crystal-receipt#218 body fix** — Two SHA-256s quoted in the PR body are a transcription slip (right 8-char prefix, divergent tail); the vendored bytes are byte-identical to canonical. Correct them to `22207f8c4047…` / `8d53ab1d3dfb…`, then the PR is clean to merge. Its merge commit becomes `effective_from` for the separate immutable registry record in `recompute-kit`.

3. **Producer lane sweep** — Confirm no producer is silently emitting the wrong serializer. Named producers are confirmed JCS; an audit that nothing else emits `encodeJsonUtf8Lf` bytes where JCS is normative (or vice-versa) would close the split with evidence rather than assumption.

## Net Status

Our side needs no producer change; the split is settled as JCS. Remaining work: documentation (1), the #218 body fix + registry record (2), and a confirming sweep (3).
