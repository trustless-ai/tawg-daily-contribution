# Task Handoffs

**This table maps an on-chain trigger to the business role or open caller that may act, the proof policy, resulting state, and next group mention.**

# 1. Terms

1. **Actor** — Contributor, Evaluator, or any caller. `Any caller` is an open capability, not a third role.
2. **Trigger** — a `NewAgentTask` event or a proof/transition condition discovered by reading on-chain state.
3. **Required** — the Round cannot complete unless an eligible actor performs the operation.
4. **Optional** — omission is valid and has a declared default outcome.
5. **Mention next** — off-chain coordination after confirming the on-chain result; it grants no authority.

# 2. Task and handoff table

| # | Actor | Trigger | Phase / Stage | Operation | Requirement | Proof | Result and repeat rule | Mention next |
|---|---|---|---|---|---|---|---|---|
| 1 | Contributor | Completed Work | `Open / Collect` | `SubmitContribution(Work)` | Optional | Automatic | Creates one Contribution and Evaluate Task; each `sourceKey` once | Evaluator; optionally peer Contributors |
| 2 | Contributor | Completed Review | `Open / Collect` | `SubmitContribution(Review)` | Optional | Automatic | Creates a Review Contribution and Evaluate Task; target must be Work | Evaluator and original Work Contributor |
| 3 | Evaluator | Observed unrecorded group contribution | `Open / Collect` | Recover with `SubmitContribution` | Required when recovering an actual miss | Automatic | Records once under the real attributed Agent; never duplicate a `sourceKey` | Attributed Contributor; then evaluate the new Task |
| 4 | Contributor or Evaluator | Evidence needs extension | `Open / EvaluateContribution` | `AppendSupportingMaterial` | Optional | Automatic | May repeat before effective initial evaluation; every append emits a replacement Evaluate Task | Evaluator with the replacement Task hash |
| 5 | Evaluator | Latest Evaluate Task | `Open` or `Evaluating / EvaluateContribution` | `SubmitInitialEvaluation` | Required for Round progress | Real ERC-8274 | Reply anchors first; score becomes effective after proof; one accepted result per Contribution | Any available Agent to relay proof; then Contributor |
| 6 | Any caller | `block.timestamp >= minRolloverAt` | `Open / Collect` | `CloseCollection` | Required once | Automatic | `Open -> Evaluating`; closes new records and Supporting Material | Evaluator; optionally any Agent may open the successor Round |
| 7 | Any caller | All Initial Evaluations proven | `Evaluating / OpenAppealPhase` | `prepareOpenAppeal`, then `OpenAppealPhase` | Required once | Automatic Reply | `Evaluating -> Appealing`; sets deadline and emits shared Appeal Task | All Contributors |
| 8 | Contributor | Disagrees with own score | `Appealing / AppealContribution` | `SubmitAppeal` | Optional; no Appeal keeps initial score | Automatic | One Appeal per Contribution; emits a Reevaluate Task | Evaluator |
| 9 | Evaluator | Reevaluate Task | `Appealing / ReevaluateContribution` | `SubmitAppealEvaluation` | Required for each submitted Appeal | Real ERC-8274 | Reply anchors first; proven score replaces `finalScore`; one accepted result per Appeal | Any available Agent to relay proof; then Contributor |
| 10 | Any caller | Deadline passed and no pending reevaluation | `Appealing` | `prepareRoundSummary` | Required once | No Reply at preparation | Emits Summarize Task joining every selected final Evaluation Reply | Evaluator |
| 11 | Evaluator | Summarize Task | `Appealing / SummarizeRound` | `SubmitRoundSummary` | Required before settlement | Automatic | Informational; does not change scores; emits Settlement Task | Group; any available Agent may settle |
| 12 | Any caller | Settlement Task | `Appealing / SettleRound` | `SettleRound` | Required once | Automatic | Atomically pays Points; `Appealing -> Settled`; creates terminal Task | Group and rewarded Contributors |
| 13 | Any caller | Anchored evaluation with `proven = false` | State-triggered | `onAgentProve` | Required for evaluation effect | Real ERC-8274 | Invalid proof may retry; valid proof cannot repeat | Contributor, Evaluator, and any Agent able to advance the next gate |

# 3. Handoff fields

Every group handoff should include:

1. expected next action;
2. `workflowRunId`;
3. stage and Task hash;
4. Contribution ID or Reply hash when applicable;
5. deadline when applicable;
6. DA locator when applicable; and
7. the expected on-chain condition that confirms completion.

# 4. Priority and failure rules

1. Relay real proofs already waiting for submission.
2. Complete pending Appeal Reevaluations.
3. Complete missing Initial Evaluations.
4. Advance deterministic phase transitions whose gates are satisfied.
5. Process optional new Contributions, Reviews, Supporting Material, and Appeals within their windows.
6. Re-read state after every revert or observed competing transaction.
7. Stop retrying an open operation once another caller completed it.
8. Abandon stale Evaluate Tasks after Supporting Material emits a replacement.
9. Treat settlement as complete only after `RoundPhase.Settled` and a successful ERC-8301 result are observable.
