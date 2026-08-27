# TAWG Modal Webhook Runtime Implementation Plan

> **Execution rule:** implement each task with a red-green-refactor cycle. Modal remains a thin adapter; all bot behavior stays callable from the existing Python CLI and GitHub Actions.

**Goal:** Add a privacy-safe Telegram webhook and Modal deployment wrapper without coupling the bot core to Modal, while preserving GitHub as the canonical state/audit store and keeping the existing Actions polling path available until cutover.

**Architecture:** A platform-neutral webhook module authenticates and normalizes Telegram updates into a strict sanitized envelope. A platform-neutral ingestion/runtime entrypoint persists envelopes and runs the existing reply pipeline. `deploy/modal_app.py` supplies only Modal endpoint, durable spawn, scheduling, checkout, timeout, concurrency, and secret wiring. GitHub Actions and the CLI call the same core entrypoints.

**Tech stack:** Python 3.12, Pydantic, httpx, pytest/pytest-asyncio, Modal Python SDK, GitHub Actions, Telegram Bot API.

---

## Task 1: Define the strict sanitized webhook contract

**Files:**

- Create: `src/tawg_bot/telegram_webhook.py`
- Create: `tests/unit/test_telegram_webhook.py`

1. Add failing tests for a valid group message, edited message, caption/media metadata, mention/command detection using UTF-16 entity offsets, and reply/thread preservation.
2. Add failing tests proving the serialized envelope contains neither the configured numeric chat ID nor numeric sender ID.
3. Add failing tests for missing/wrong secret headers, wrong chat, private chat, unsupported update types, malformed update IDs, oversized bodies/text/attachments, and privacy-rejected secrets.
4. Implement immutable Pydantic models for the versioned envelope and safe attachment/entity metadata.
5. Implement a pure `TelegramWebhookNormalizer` that accepts explicit configuration and `PrivacyFilter`; authenticate with `hmac.compare_digest`, validate the fixed group, sanitize content/display name, discard unexpected fields, and compute a deterministic integrity digest.
6. Return explicit safe dispositions (`dispatch`, `ignore`, `reject`) without logging raw input or exceptions.
7. Run:

   ```bash
   python -m pytest tests/unit/test_telegram_webhook.py -q
   python -m ruff check src/tawg_bot/telegram_webhook.py tests/unit/test_telegram_webhook.py
   ```

8. Commit: `feat: add privacy-safe Telegram webhook envelope`

## Task 2: Extract common Telegram persistence from polling intake

**Files:**

- Modify: `src/tawg_bot/telegram_intake.py`
- Modify: `src/tawg_bot/models.py`
- Modify: `tests/integration/test_telegram_cursor.py`
- Create: `tests/integration/test_telegram_webhook_intake.py`

1. Add failing tests that ingest sanitized envelopes without a Telegram API client or polling cursor.
2. Require all configured-group messages to persist as context, with only mentions/commands creating jobs.
3. Add replay and out-of-order tests: the same `update_id` is a no-op, an older unseen update still persists, and a duplicate mention never creates a second job.
4. Add edited-message tests preserving stable record/job identity and updating safe message content/metadata.
5. Add a bounded webhook receipt state model under `data/state/telegram-webhook-receipts.json`; keep enough update IDs for deduplication without using a maximum offset that can skip out-of-order updates.
6. Extract a common message persistence service used by both `TelegramIntake.collect()` and the new `ingest_envelopes()` entrypoint.
7. Preserve the polling path's current cursor behavior and existing fixture results exactly.
8. Ensure envelope ingestion, pending jobs, aliases, receipt state, and message records publish atomically through one `RepositoryUnitOfWork`.
9. Run:

   ```bash
   python -m pytest tests/integration/test_telegram_cursor.py tests/integration/test_telegram_webhook_intake.py -q
   ```

10. Commit: `refactor: share Telegram polling and webhook persistence`

## Task 3: Add platform-neutral webhook and maintenance runtime entrypoints

**Files:**

- Modify: `src/tawg_bot/runtime.py`
- Modify: `src/tawg_bot/scheduler.py`
- Modify: `src/tawg_bot/cli.py`
- Modify: `tests/unit/test_scheduler.py`
- Modify: `tests/integration/test_runtime_composition.py`
- Create or modify: `tests/unit/test_cli.py`

1. Add failing scheduler tests for a maintenance tick that skips `telegram_intake` but retains source, knowledge, Daily, reply preparation, validation, checkpoint, and delivery behavior.
2. Add failing runtime tests for `ingest_webhook_envelope()`: persist/checkpoint intake first, process the bounded reply batch, preserve reply/thread targets, and replay safely after partial success.
3. Add failing tests that webhook mode never calls `getUpdates`, while explicit polling mode still does.
4. Add a `ProductionRuntime.ingest_webhook_envelope(...)` entrypoint using only ordinary Python values and existing checkpoint abstractions.
5. Add `ProductionRuntime.maintenance_tick(...)` and a scheduler intake policy rather than branching on Modal-specific environment state.
6. Extend the CLI with explicit `ingest-webhook-envelope` and `maintenance-tick` operator commands suitable for GitHub Actions/replay. Read envelope JSON from a confined file or stdin without echoing content.
7. Keep `tick` as the polling/manual-fallback command and refuse ambiguous mixed modes.
8. Run:

   ```bash
   python -m pytest tests/unit/test_scheduler.py tests/integration/test_runtime_composition.py tests/unit/test_cli.py -q
   ```

9. Commit: `feat: add platform-neutral webhook runtime entrypoints`

## Task 4: Add isolated repository session orchestration

**Files:**

- Create: `src/tawg_bot/repository_session.py`
- Create: `tests/unit/test_repository_session.py`
- Modify: `scripts/commit_operation.sh`
- Modify: `tests/unit/test_workflow_config.py`

1. Add failing tests for a fresh branch checkout, allowlisted checkpoint invocation, non-fast-forward retry, cleanup, and sanitized error reporting using fake command runners.
2. Define a small injected command-runner protocol so tests execute no network writes and no repository binaries.
3. Implement a platform-neutral repository session that prepares a fresh workspace and invokes the existing restricted checkpoint; it must not import Modal or know where credentials came from.
4. Preserve the current `data/**` and `knowledge/**` write allowlist and non-force-push behavior.
5. Add safe retry from a newly fetched `main` after an optimistic conflict; never reset or overwrite a user's working tree.
6. Run:

   ```bash
   python -m pytest tests/unit/test_repository_session.py tests/unit/test_workflow_config.py -q
   ```

7. Commit: `feat: add isolated repository operation sessions`

## Task 5: Implement the thin Modal adapter

**Files:**

- Create: `deploy/modal_app.py`
- Create: `deploy/__init__.py`
- Create: `tests/unit/test_modal_adapter.py`
- Modify: `pyproject.toml`
- Update: `requirements-dev.lock`

1. Add a failing isolation test that imports every `tawg_bot` module with the Modal import blocked; core imports and CLI parsing must succeed.
2. Add adapter contract tests with a fake `modal` module, fake request, fake durable dispatcher, and fake repository session.
3. Require the endpoint to validate a bounded body and Telegram secret, normalize synchronously, pass only `TelegramWebhookEnvelope.model_dump(mode="json")` to `spawn()`, and return success only after durable dispatch acceptance.
4. Add tests that inspect the spawned payload and logs for raw chat/sender IDs, secrets, raw body fragments, and tokenized Telegram URLs.
5. Implement `deploy/modal_app.py` with the Modal SDK import isolated to that file. Declare:

   - a fast web endpoint with a short timeout;
   - a durable background worker with one active container and bounded timeout;
   - a scheduled maintenance entrypoint; and
   - Modal secrets/image configuration.

6. The worker creates a fresh repository session and calls the core `ingest_webhook_envelope()` entrypoint. The scheduled function calls the core `maintenance_tick()` entrypoint. No bot decisions or persistence logic may appear in the adapter.
7. Put Modal/FastAPI dependencies in a dedicated optional dependency group so normal package installation and GitHub Actions core operation do not require Modal.
8. Run:

   ```bash
   python -m pytest tests/unit/test_modal_adapter.py tests/unit/test_cli.py -q
   python -m mypy src/tawg_bot
   ```

9. Commit: `feat: add isolated Modal deployment adapter`

## Task 6: Add CI/deployment and safe mode controls

**Files:**

- Create: `.github/workflows/modal-deploy.yml`
- Modify: `.github/workflows/tawg-knowledge.yml`
- Modify: `tests/unit/test_workflow_config.py`
- Modify: `docs/operator/github-actions.md`

1. Add failing workflow tests for pinned Python/runtime dependencies, least permissions, concurrency, required deployment secrets, and no token printing.
2. Add an explicit runtime mode variable with `poll`, `webhook`, and `observe` semantics. During shadow rollout, keep the current five-minute polling schedule unchanged.
3. Add a verified deployment workflow that runs Ruff, mypy, full pytest, and vault lint before `modal deploy`. Do not configure Telegram's webhook automatically.
4. Ensure manual Actions fallback refuses polling when the operator marks webhook mode active.
5. Document the GitHub and Modal secrets/variables without including values.
6. Run:

   ```bash
   python -m pytest tests/unit/test_workflow_config.py -q
   ```

7. Commit: `ci: add verified Modal deployment path`

## Task 7: Document shadow rollout, cutover, and rollback

**Files:**

- Modify: `docs/operator/runbook.md`
- Modify: `docs/operator/rollout.md`
- Modify: `docs/operator/manual-testing.md`
- Create: `docs/operator/modal.md`

1. Document `uv run --with modal modal deploy deploy/modal_app.py` and the authenticated local setup path without embedding credentials.
2. Document secret creation through an interactive local command or Modal dashboard; instruct operators never to paste secrets into chat or commit them.
3. Add a shadow validation checklist using a synthetic signed fixture that contains no real Telegram identifiers and invokes no paid model.
4. Add explicit `setWebhook` verification with a secret token and `drop_pending_updates=false`.
5. Require confirmation of one real repository record and correctly threaded delivery before disabling the GitHub polling schedule.
6. Add rollback: `deleteWebhook` with `drop_pending_updates=false`, confirm no active webhook, restore polling, and reconcile pending work.
7. Update older runbook statements that currently require webhook to be unset, while preserving that rule for polling mode.
8. Run:

   ```bash
   python -m tawg_bot.cli vault-lint
   rg -n "getUpdates|webhook|drop_pending_updates|modal" docs/operator
   ```

9. Commit: `docs: add Modal webhook operations guide`

## Task 8: Full local verification and reviews

**Files:** all changed files

1. Run formatting/static checks:

   ```bash
   python -m ruff check src tests deploy
   python -m mypy src/tawg_bot
   ```

2. Run the full suite and vault lint:

   ```bash
   python -m pytest -q
   python -m tawg_bot.cli vault-lint
   git diff --check origin/main...HEAD
   ```

3. Run a deterministic Action-equivalent polling tick in observe-only mode with fakes/fixtures and confirm it does not import Modal.
4. Run the synthetic signed webhook fixture locally and confirm exactly one sanitized envelope is offered to the fake durable dispatcher.
5. Request Python code review and security review. Fix findings with new regression tests first.
6. Re-run every gate after review fixes.
7. Commit: `test: verify Modal webhook runtime boundaries`

## Task 9: Push through GitHub MCP and shadow-deploy

1. Inspect the complete feature diff and commit list against `origin/main`.
2. Push the feature branch only through GitHub MCP. Never use local `git push`, `gh`, or a direct GitHub API write.
3. Verify the remote commit contains all intended files, then synchronize locally with read-only fetch.
4. Create/review the integration path through GitHub MCP as requested by the operator.
5. After the verified code reaches `main`, deploy the exact commit to Modal using the authenticated `uv run --with modal` path.
6. Do not configure Telegram webhook and do not disable the GitHub schedule yet.
7. Send one synthetic signed fixture, inspect safe Modal logs, confirm durable dispatch/idempotency/maintenance health, and record the deployed app URL and commit.

## Task 10: Controlled production cutover

1. Present the shadow evidence and exact cutover commands to the operator before external state changes.
2. Configure the Telegram webhook only after explicit operator authorization.
3. Confirm webhook status, then send/observe one real group update.
4. Verify one canonical source record, correct job state, one correctly threaded Telegram response, no duplicate, and no unsafe log content.
5. Disable the GitHub polling schedule through GitHub MCP only after webhook confirmation.
6. Verify Modal maintenance schedules and pending/retryable job state.
7. If any acceptance check fails, execute the documented no-drop rollback and restore polling before further diagnosis.
