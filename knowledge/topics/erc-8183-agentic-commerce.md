---
title: "ERC-8183 Agentic Commerce"
type: topic
created: "2026-08-29"
updated: "2026-08-29"
source_ids:
- "tg:tawg:3620"
- "tg:tawg:3650"
telegram_record_ids:
- "tg:tawg:3620"
- "tg:tawg:3650"
source_urls:
- "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902"
- "https://eips.ethereum.org/EIPS/eip-8183"
provenance_status: verified
---

# ERC-8183: Agentic Commerce

ERC-8183 proposes an on-chain lifecycle protocol for task-based agentic commerce. It defines a state machine with transitions through Open, Funded, Completed, Rejected, and Expired states, intended to standardize how agents post, fund, and evaluate jobs on-chain.

## Community Discussion Themes

The Ethereum Magicians discussion (page 1, posts through March 2026) surfaced four main threads:

**Design critiques:** Participants questioned whether the Open to Funded split adds avoidable on-chain overhead versus leaving that to implementations, and whether the near-identical Rejected and Expired states unnecessarily close off a reopen-to-Funded path. Comparisons were drawn to OZ ConditionalEscrow and Alkahest as potentially conformant implementations under a less opinionated spec.

**Naming debate:** Several contributors argued that the protocol encodes general escrow logic rather than anything distinctively agentic, noting that a human and AI initiator are treated identically by the design.

**Evaluator patterns:** Production practitioners emphasized the evaluator as the primary complexity surface. Approaches discussed include multi-model consensus thresholds, statistical calibration experiments, reputation gating before funding to deter low-effort submissions, and asynchronous off-chain evaluation triggered by on-chain events within the `expiredAt` window.

**ERC-8004 reputation integration:** The most prominent integration theme was writing reputation signals to ERC-8004 registries on `complete` or `reject` events, either directly from the evaluator or via `afterAction` hooks, creating a verification to payment to reputation loop.

The spec remains at **Draft** status. The forum thread extends beyond page 1.

## Sources

- https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902
- https://eips.ethereum.org/EIPS/eip-8183
