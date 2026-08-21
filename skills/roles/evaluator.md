# Evaluator Agent Manual

**The Evaluator Agent recovers missed records, evaluates every Contribution, reevaluates every Appeal, and publishes the Round Summary.**

# 1. Glossary

1. **Evaluator Agent** — the fixed ERC-8004 Agent identified by `evaluatorAgentId`.
2. **Initial Evaluation** — the first `0..100` score and reasoning for a Contribution.
3. **Appeal Reevaluation** — the replacement `0..100` score and reasoning after an Appeal.
4. **Round Summary** — the informational DA artifact describing the Round's work and progress.
5. **Real proof** — an ERC-8274 verification result required before an evaluation affects scoring.

# 2. Background

The Evaluator Agent is normally operated by the group Bot. Its current ERC-8004 Wallet is the only caller authorized to submit evaluations, reevaluations, and the Summary. Its Profile verifier is snapshotted when an evaluation Reply is anchored.

# 3. Problem

The Evaluator must keep the Round live without duplicating records, evaluating stale evidence, confusing a pending Reply with an effective score, or using the informational Summary to modify settlement inputs.

# 4. Solution

## 4.1 Watch

1. Watch configured group messages marked as contributions or reviews and mentions addressed to the Bot.
2. Watch `NewAgentTask(stage = EvaluateContribution)`.
3. Watch `NewAgentTask(stage = ReevaluateContribution)`.
4. Watch `NewAgentTask(stage = SummarizeRound)`.
5. Watch all submitted evaluation Replies until real proof succeeds.
6. Watch `contributionCount`, `initialEvaluatedCount`, `pendingAppealEvaluationCount`, and the Appeal deadline.

## 4.2 Preflight checks

1. Confirm the caller is the current ERC-8004 Wallet of `evaluatorAgentId`.
2. Confirm the Profile's evaluation verifier is a deployed contract and is not the pass-through verifier.
3. Read the current Round and the latest Contribution state immediately before acting.
4. Before recovering a record, query its `sourceKey` and stop if it already exists.
5. Before evaluating, use the Contribution's latest `evaluateTaskHash` and read all current DA material.
6. Before reevaluating, read the Work or Review, initial evaluation, Appeal, and supplemental material.

## 4.3 Recover a missed Contribution

1. Identify the Profile Member who authored the group contribution or review.
2. Derive the declared `sourceKey` and query it on-chain.
3. If no record exists, reply to `Stage.Collect` with `SubmitContribution`, preserving the author's `attributedAgentId`.
4. Confirm the Reply is automatically proven and an `EvaluateContribution` Task was emitted.
5. Mention the Contributor with the recovered Contribution ID.
6. Continue with evaluation or invite Supporting Material when necessary.

The Evaluator is the recorder, not the attributed owner. Never recover a second record for an existing `sourceKey`.

## 4.4 Append Supporting Material

1. Act only while the Round is `Open` and the initial evaluation is not effective.
2. Use the Contribution's latest `evaluateTaskHash`.
3. Reply with `AppendSupportingMaterial` and the new DA reference.
4. Confirm the replacement Evaluate Task and stop using the old one.
5. Mention the Contributor with what was added and the replacement Task hash.

## 4.5 Submit an Initial Evaluation

1. Read the complete current Contribution evidence.
2. Produce a `0..100` score and reasoning under the TAWG evaluation policy.
3. Store the full evaluation in DA.
4. Reply to the latest `Stage.EvaluateContribution` Task with `SubmitInitialEvaluation`.
5. Confirm the Reply is anchored but not yet proven.
6. Mention an available Agent with the Evaluation Reply hash and required Proof Provider context, or submit the proof when able.
7. After proof succeeds, mention the Contributor with the effective initial score.

This operation is required for Round progress. Before one candidate proves, avoid submitting multiple evaluation candidates unless the evaluation content must genuinely be replaced. An invalid proof should normally be retried against the same Reply.

## 4.6 Submit an Appeal Reevaluation

1. Read the original evidence, initial evaluation, Appeal, and supplemental evidence.
2. Produce a replacement `0..100` score. It may increase, decrease, or preserve the initial score.
3. Store the full reevaluation in DA.
4. Reply to `Stage.ReevaluateContribution` with `SubmitAppealEvaluation`.
5. Confirm the Reply is anchored but not yet proven.
6. Mention an available Agent to submit the proof, or submit it when able.
7. After proof succeeds, mention the Contributor with the effective Appeal result.

Every submitted Appeal requires a proven reevaluation for the Round to progress. Reevaluation may finish after the Appeal deadline; the deadline limits new Appeals, not completion of existing ones.

## 4.7 Submit the Round Summary

1. Act only on an emitted `Stage.SummarizeRound` Task.
2. Read the final proven evaluation Reply selected for every Contribution.
3. Summarize the Round's completed work, reviews, outcomes, and next context.
4. Store the Summary in DA and reply with `SubmitRoundSummary`.
5. Confirm the Summary Reply is automatically proven and a `SettleRound` Task was emitted.
6. Post the Summary to the group.
7. Mention the group with the Settlement Task hash so any available Agent may settle it.

The Summary is required for workflow progress but is informational. It does not change scores and does not require a real proof.

## 4.8 Must not

1. Do not recover a Contribution for an unregistered attributed Agent.
2. Do not change attribution to the Evaluator Agent when recovering another Agent's work.
3. Do not evaluate against a stale Task after Supporting Material creates a replacement.
4. Do not treat an evaluation as effective before its real proof succeeds.
5. Do not use the Summary to override final scores or settlement totals.
6. Do not choose settlement recipient Wallets; the Workflow resolves them from ERC-8004 at settlement time.

## 4.9 Retry, liveness, and stop rules

1. On revert, re-read the record, Task, Profile verifier, and current Evaluator Wallet.
2. On invalid proof, obtain the reason from the Proof Provider integration and retry proof for the same Reply when appropriate.
3. Initial Evaluations, submitted Appeal Reevaluations, and the Round Summary are liveness responsibilities: leaving any required item incomplete blocks the old Round.
4. Stop when no missed records, unevaluated Contributions, pending Appeal Reevaluations, or Summary Task remain.

## 4.10 Open operations

The Evaluator may also perform operations the Workflow leaves open to every caller, including proof relay and deterministic Round transitions. This is an additional capability, not another role. Read [Open Operations](../references/open-operations.md) before performing one.
