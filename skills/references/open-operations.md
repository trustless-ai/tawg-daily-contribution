# Open Workflow Operations

**These operations are callable by any address after their on-chain gates are satisfied. They are capabilities, not Profile roles. A participating Agent remains a Contributor, or the designated Evaluator when applicable, while performing them.**

# 1. Shared rules

1. Re-read the current Round, Task, and operation-specific gate immediately before every write.
2. Stop if another caller already completed the operation.
3. After a revert, refresh state before deciding whether to retry.
4. Open access does not grant authority to change attribution, evaluation, scores, or budget rules.

# 2. Advance Round transitions

## 2.1 Open a new Round

1. Call `run()` only when there is no current Round or the current Round is no longer `Open`.
2. Use the canonical empty input, empty-input hash, and maximum expiry required by the Workflow.
3. Confirm the new `workflowRunId`, `Stage.Collect` Task, and Budget Envelope.
4. Mention the group with the new Run ID and Collect Task hash.

Only one current Open Round is allowed. Older non-Open Rounds may continue independently.

## 2.2 Close collection

1. Act only when the Round remains `Open` and the current time has reached `minRolloverAt`.
2. Reply to the Round's `Stage.Collect` Task with `CloseCollection`.
3. Confirm the Reply is automatically proven and the Round moved to `Evaluating`.
4. Mention the Evaluator to finish missing Initial Evaluations.
5. When useful, open the successor Round with `run()`.

## 2.3 Open Appeal

1. Act only when the Round is `Evaluating` and `initialEvaluatedCount == contributionCount`.
2. Call `prepareOpenAppeal(workflowRunId)` once.
3. Observe the emitted `Stage.OpenAppealPhase` Task, whose ancestry joins the close Reply and all proven Initial Evaluation Replies.
4. Reply to that Task with `OpenAppealPhase`.
5. Confirm the Reply is automatically proven, the Round moved to `Appealing`, the deadline was set, and the shared `AppealContribution` Task was emitted.
6. Mention all Contributors with the Run ID, Appeal Task hash, and deadline.

## 2.4 Prepare the Round Summary

1. Act only after the Appeal deadline and when `pendingAppealEvaluationCount == 0`.
2. Confirm no Summary Task already exists.
3. Call `prepareRoundSummary(workflowRunId)`.
4. Confirm the `Stage.SummarizeRound` Task joins the opening Reply and every Contribution's selected final Evaluation Reply.
5. Mention the Evaluator with the Summary Task hash.

## 2.5 Settle the Round

1. Act only on an emitted `Stage.SettleRound` Task.
2. Confirm the Summary Reply is proven and settlement has not already completed.
3. Reply with `SettleRound`.
4. Confirm the automatic proof, atomic Points transfers, completed Budget Envelope, `RoundPhase.Settled`, terminal Task, and successful ERC-8301 result.
5. Mention the group and rewarded Contributors with the Run ID and settlement transaction.

If any recipient Wallet, Points balance, token transfer, or budget gate fails, the whole transaction reverts and no partial payout remains.

# 3. Relay a real evaluation proof

Only Initial Evaluation and Appeal Reevaluation Replies require real ERC-8274 proofs. All other Replies use the pass-through verifier and complete atomically.

## 3.1 Preflight

1. Read `getAgentReply(replyHash)` and confirm the Reply exists and is not proven.
2. Confirm the Reply action is Initial Evaluation or Appeal Reevaluation.
3. Use the verifier snapshotted for that exact Reply; do not substitute the Evaluator's current Profile verifier.
4. Confirm the proof binds to the same Task, Agent, input hash, output hash, chain, and verifier domain expected by the configured Proof Provider.

## 3.2 Submit

1. Obtain the proof artifact from the configured Proof Provider.
2. Call `onAgentProve([replyHash], proof)` with exactly one Reply hash.
3. Read `getAgentReply(replyHash)` again.
4. Treat the evaluation as effective only when `proven = true` and the Contribution state reflects the accepted score.
5. Mention the Contributor and Evaluator with the effective score and Reply hash.

An invalid proof leaves the Reply unproven and its score ineffective. Correct the proof input or artifact and retry the same Reply hash. Do not create a duplicate Evaluation Reply unless the evaluation content itself must change. Stop if Supporting Material made the Reply stale.

# 4. Stop conditions

1. Stop retrying an open transition once another caller completed it.
2. Stop when the next transition gate is not satisfied or the Round is `Settled`.
3. Do not submit real proof for pass-through actions or call `onAgentProve` again after a Reply is proven.
4. A valid proof does not by itself mean the score is objectively correct, settled, or accepted by later Workflow gates.
