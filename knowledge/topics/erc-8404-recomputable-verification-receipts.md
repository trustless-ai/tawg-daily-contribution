---
title: "ERC-8404 – Recomputable Verification Receipts"
type: topic
created: "2026-08-31"
updated: "2026-08-31"
source_ids:
- "tg:tawg:3808"
- "tg:tawg:3800"
- "tg:tawg:3793"
telegram_record_ids:
- "tg:tawg:3808"
- "tg:tawg:3800"
- "tg:tawg:3793"
source_urls:
- "https://github.com/ethereum/ERCs/pull/1980"
- "https://ethereum-magicians.org/t/erc-8404-recomputable-verification-receipts/29521"
provenance_status: verified
---

# ERC-8404 – Recomputable Verification Receipts

ERC-8404 is the officially assigned number for the Recomputable Verification Receipts (RVR) proposal. Ethereum ERC editors assigned this number to the submission; the canonical filename is `ERCS/erc-8404.md` and the Ethereum Magicians discussion title and URL were updated to match.

## Status

As of 2026-08-31, exact-head ERC CI is fully green, passing both the EIP Validator and HTMLProofer checks.

- **ERC PR:** https://github.com/ethereum/ERCs/pull/1980
- **Discussion:** https://ethereum-magicians.org/t/erc-8404-recomputable-verification-receipts/29521

## Core concept

RVR defines a standard for verification receipts that any independent party can recompute from committed inputs. A verifier replays the same deterministic process rather than trusting a reported result; a receipt is valid when the independently derived output matches the committed one.

## recompute-kit PR #36

The namespace-opacity rule and failing-witness shape in recompute-kit PR #36 are confirmed correctly scoped for this proposal. As of 2026-08-31, the PR has a merge conflict with main; a branch refresh is required before the reference 10/10 recomputation can be confirmed and the namespace-sensitive mutant failure at 9/10 verified.

The normative rule: an enforcer MUST NOT condition its verdict on schema namespace; only the profile governs. The failing witness duplicates the first pre-cutoff case with only the binding's schema swapped to a foreign namespace, asserting an identical ADMIT.

## Sources

- https://github.com/ethereum/ERCs/pull/1980
- https://ethereum-magicians.org/t/erc-8404-recomputable-verification-receipts/29521
