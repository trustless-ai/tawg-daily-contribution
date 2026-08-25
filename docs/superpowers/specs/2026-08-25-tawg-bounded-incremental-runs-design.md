# Bounded Incremental Action Runs

## Goal

Keep every scheduled bot run below ten minutes while preserving completed work after each bounded batch. A slow or malformed source, knowledge refresh, reply, or Daily preparation must not discard unrelated progress or prevent later work from running.

## Runtime contract

- The bot's internal phase budgets are designed to finish each operation within ten minutes even when the outer runner timeout is looser.
- The bot commits and pushes Telegram intake before model work.
- Scheduled source checks process at most two ERCs, with a one-minute per-ERC timeout and one independently persisted checkpoint per ERC.
- Scheduled knowledge refresh processes at most one ERC. Model time is bounded below the Action timeout.
- Reply preparation processes at most one pending mention per run. Ready replies go first; otherwise the oldest pending job goes first, so a recent failure rotates behind other waiting mentions.
- Daily preparation has priority on L4 runs, uses a 360-second model timeout, and defaults to `medium` effort even when the provider-wide effort is higher.
- Daily evidence is selected into bounded per-source quotas and a fourteen-item total while retaining one high-signal item per contributor where space permits. One unavailable external source does not discard evidence from Telegram or another source.
- A recoverable phase failure is logged using only a fixed phase name, bounded identifier, and safe error code. Raw exception messages, provider output, source bodies, and credentials are never logged.
- Recoverable failures do not abort later phases. Failed knowledge work is deferred with bounded exponential backoff, so another ERC can advance on the next run.
- A failed Daily remains retryable: L4 success is not recorded until Daily delivery completes.
- Once a Daily has been prepared in a run, already-ready replies may deliver but no new reply model job starts; pending mentions rotate to the next five-minute tick.
- Checkpoint or validation failures do not claim the affected layer as successful.

## Persistence and idempotency

Repository state remains the only durable cross-run context. Telegram cursors, source observations, refresh jobs, prepared replies, prepared Daily artifacts, delivery states, and layer-success timestamps are committed through the existing restricted checkpoint script. External source bodies are transient and are never added to the repository.

Each checkpoint operation ID is deterministic enough for auditability and restricted to the existing safe character set. Replaying a batch uses the current repository state and remains idempotent.

## Failure handling

Operational exceptions are converted to stable codes such as `source_check_failed`, `knowledge_refresh_failed`, and `daily_prepare_failed`. Knowledge failures increment `retry_count`, update the job timestamp, and become eligible after a capped delay. The Action log reports the deferral and continues.

Programming, privacy, and invariant failures are also represented without their raw messages. Validation failure prevents success-state advancement even though the process can finish cleanly and retry later.

## Daily guarantee

At 23:00 UTC, the L4 run still starts with Telegram intake, but optional source and knowledge work are bounded. Daily preparation and delivery are attempted in the same run. If they cannot complete safely, the Daily window stays due and the next scheduled run retries it without duplicating an already delivered message.

The model receives representative evidence rather than every raw body in the window. Concrete bullets keep exact allowlisted citations. An uncited direction synthesis may describe generic progress, status, review, test, or implementation work, but cannot contain contributor names, numbers, URLs, citations, or source-specific artifact identifiers.
