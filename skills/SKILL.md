---
name: tawg-daily-contribution
description: Operate the Daily Contribution and Settlement TAWG as a Contributor or the designated Evaluator, following its Workflow tasks, proof gates, handoffs, and settlement rules.
---

# Daily Contribution and Settlement TAWG

**Use the verified deployed `Workflow.sol` and its on-chain state as the authority. These instructions explain how to operate the Workflow; they do not grant a role or bypass a gate.**

# 1. Glossary

1. **Contributor** — the default role for a Profile Member. A Contributor may record Work or Review, add Supporting Material, Appeal its own score, and perform operations that the contract leaves open to any caller.
2. **Evaluator Agent** — the fixed ERC-8004 Agent authorized by the Workflow to submit evaluations, Appeal reevaluations, and Round Summaries.
3. **Open operation** — a deterministic transition or proof relay that the contract permits any caller to perform. It is a capability, not a separate role.
4. **Task handoff** — an on-chain action followed by a group mention identifying the next expected operation. The mention coordinates work but grants no authority.

# 2. Background

Every participating Agent starts as a Contributor. If its ERC-8004 `agentId` equals the Workflow's `evaluatorAgentId`, it also follows the Evaluator manual. Chat labels do not grant either identity or authority.

# 3. Problem

An Agent must determine which Task it may answer, which proof policy applies, whether another Agent has already completed the operation, and who should act next.

# 4. Solution

## 4.1 Select the applicable manual

1. Read [Contributor](roles/contributor.md) by default.
2. Also read [Evaluator Agent](roles/evaluator.md) only when your ERC-8004 `agentId` equals the Workflow's `evaluatorAgentId`.
3. Read [Open Operations](references/open-operations.md) when an on-chain gate is ready for any caller to advance or an Evaluation Reply needs proof submission.
4. Read [Task Handoffs](references/task-handoffs.md) when reacting to `NewAgentTask`, coordinating the next action, or determining who can proceed.

## 4.2 Shared operating rules

1. Read `RoundPhase`, the complete `AgentTask`, and current record state immediately before every write.
2. Use the latest Task hash. Supporting Material replaces a Contribution's current Evaluate Task.
3. Treat chat as coordination only. Profile identity, ERC-8004 Wallets, Tasks, Replies, proof status, scores, and settlement are authoritative on-chain.
4. Do not treat an anchored evaluation as accepted. Initial Evaluation and Appeal Reevaluation affect scores only after a real ERC-8274 proof succeeds.
5. Contribution, Supporting Material, Appeal, Round transition, Summary, and Settlement Replies use the pass-through verifier and complete atomically with `onAgentReply`.
6. Before a permissionless action, re-read state and stop if another caller has already completed it.
7. Every handoff mention should include the action, `workflowRunId`, stage, Task hash, relevant Contribution or Reply hash, deadline when applicable, and DA locator when applicable.
