# Bounded Incremental Action Runs Implementation Plan

1. Add regression tests that require bounded source, knowledge, Daily, and reply phases, two-source and one-reply scheduled batches, per-batch checkpoints, safe continuation after failures, and a retryable Daily.
2. Add knowledge-job deferral with capped backoff and test that a failed ERC cannot monopolize every run.
3. Make scheduled source and knowledge work persist and checkpoint independently, with safe structured logging that excludes exception text.
4. Make the scheduler continue after recoverable phase failures while recording only layers whose own bounded phase succeeded; never mark a failed Daily as delivered.
5. Bound model timeouts and pending reply work so the runtime exits before the Action hard timeout.
6. Run targeted red/green cycles, then Ruff, mypy, the full pytest suite, vault lint, local Action-equivalent simulation, and a changed-code security review.
7. Push only through GitHub MCP, synchronize the local branch read-only, update the rollout monitor, and keep polling/fixing until a real run on the intended commit succeeds.
