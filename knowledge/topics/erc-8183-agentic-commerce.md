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

ERC-8183 proposes an on-chain lifecycle protocol for task-based agentic commerce. It defines an escrow-backed job state machine with roles for a client, provider, and evaluator, standardising how agents post, fund, and settle work on-chain. The spec is at **Draft** status.

## State Machine

Jobs progress through six states: Open, Funded, Submitted, Completed, Rejected, and Expired. The client funds work; the provider submits a deliverable; the evaluator alone may mark the job completed or rejected once Submitted. Expiry allows anyone to trigger a refund after `expiredAt` passes.

## Community Discussion Themes

The Ethereum Magicians discussion has at least 394 posts as of 2026-08-24. Major active threads:

**job.reason as a property-contract:** Discussion converged on treating `job.reason` as a property-contract requiring the value to be canonical, recomputable, tamper-evident, and attributable — with interpretation left to the consumer. Two independent production implementations reached this framing from different starting points: one proof-based (ERC-8274 verificationDigest binding) and one behavioral (Sentinel Oracle: keccak256 over JCS-canonicalized evidence shipped as a signed JWS). Both satisfy the same contract without importing each other's format. A v0.2 spec pass is planned to encode this as an explicit property-contract with named reference bindings and a non-interpretation non-goal.

**Evaluator availability and grace period:** A structural gap was identified: `claimRefund` is callable when status is Submitted, meaning a provider who delivers work loses payment if the evaluator fails to respond before `expiredAt`. The client sets both `expiredAt` and the evaluator address at creation; neither is changeable afterward. Discussion converged on adding `evaluationWindow` at `createJob`, materializing a separate `evaluationDeadline` at `submit()`, and gating `claimRefund` on `block.timestamp > max(expiredAt, submittedAt + GRACE_PERIOD)`. This preserves `expiredAt` as the immutable client commitment while giving the evaluator a real response window. `GRACE_PERIOD` should be a protocol-level constant, not per-job configurable.

**Discriminated expiry and submission bond:** `JobExpired` currently emits no discriminator between `Funded→Expired` (provider never submitted) and `Submitted→Expired` (evaluator never responded), preventing downstream reputation systems from scoring the correct party. Without a cost to enter Submitted, a provider can post junk one block before expiry to manufacture an EvaluationTimeout record at zero cost. The thread concluded that discriminated expiry and a separate evaluation window belong in the minimal core, and that a submission-side bond — burned rather than redistributed, to prevent slash-manufacturing incentives — is also a core property: it is the precondition for the discriminator to constitute interpretable evidence. Bond magnitude and settlement policy belong in profiles.

**ThoughtProof deployment correction:** ThoughtProof issued a public self-correction to their March 2026 forum posts (posts #14, #21, #36). Their re-audit found: the cited Base mainnet evaluator contract had zero post-deploy calls from deployment through chain head; its deployed bytecode was pre-two-phase and lacked two-phase and ERC-8004 reputation selectors; the cited settlement transaction was signed by an EOA rather than the evaluator contract; and the v1.3/two-phase/reputation description in post #36 was premature by four days relative to the deployment that matched that bytecode shape. The correction was independently reproduced by a third-party reviewer.

**On-chain forensics:** An independent index of the predecessor protocol (Virtuals ACP, Base mainnet, 62,953 jobs) found: 72.5% of jobs had no evaluator set; 27.48% used client-as-evaluator; 0.02% used an independent evaluator; 76.1% of settled volume was self-evaluated. Of 398 jobs submitting a hash of the empty string as deliverable, 98.49% were approved and paid. On ERC-8004 across both mainnets: 419,155 reputation feedback events were recorded, but the ValidationRegistry received only 12 requests and 7 responses from a single validator (the agent's own owner). A proposal was raised to link ERC-8183 settlement to an ERC-8004 validation entry — a `requestHash` committed at decision time and resolvable later — making evaluator quality claims testable after the fact.

**ERC-8378 parametric token composition:** A proposal described using ERC-8378 (Parametric Token) as `paymentToken` to settle the provider's reward post-completion. The evaluator confirms actual performance parameters on-chain; the token applies a mutation rule at transfer incorporating the confirmed value. No changes to ERC-8183 interfaces are required; the token type is detectable via ERC-165.

**Early discussion (March 2026, page 1):** Design critiques questioned the Open→Funded split overhead and near-identical Rejected/Expired states, comparing the spec to OZ ConditionalEscrow and Alkahest as potentially conformant implementations under a less opinionated design. A naming debate argued the protocol encodes general escrow logic rather than anything distinctively agentic, noting that a human and AI initiator are treated identically by the design. Evaluator pattern discussions covered multi-model consensus thresholds, reputation gating before funding to deter low-effort submissions, and asynchronous off-chain evaluation within the `expiredAt` window. Writing reputation signals to ERC-8004 registries on `complete` or `reject` events was the most prominent early integration theme.

## Sources

- https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902
- https://eips.ethereum.org/EIPS/eip-8183
