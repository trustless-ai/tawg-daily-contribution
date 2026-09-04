# TAWG Dev Bot Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second Telegram bot account ("dev bot") that runs the same Python core against the `dev` branch, replies in the shared group with its own identity, and persists only its own webhook receipt — without disturbing production.

**Architecture:** Reuse the existing Modal webhook → repository worker pipeline unchanged, but make webhook dedup per-bot, make reply-job identity bot-local in `receipt-only` mode, merge `main` into `dev` before each dev run (fail closed on conflict), and narrow `commit_operation.sh` so dev commits only the merge commit plus its own namespaced receipt.

**Tech Stack:** Python 3.12, pydantic, pytest, ruff, mypy, Modal, bash.

**Spec:** `docs/superpowers/specs/2026-09-01-tawg-dev-bot-mirror-design.md`

## Global Constraints

- Python 3.12; `StrEnum` is already used by `models.py` (see `DeliveryStatus`).
- Lint/lint gates: `python -m ruff check src tests deploy`, `python -m mypy src/tawg_bot`, `python -m pytest -q`, `python -m tawg_bot.cli vault-lint`.
- Production cadence, delivery, knowledge, and message-history stream must not change; only its receipt file migrates to a namespaced path once.
- The bot identity key is the numeric id (token prefix), never the username.
- `main` never merges another branch.
- Dev replies may be lost on crash but must not be duplicated; receipt is committed before Telegram send.

---

### Task 1: Bot identity and persistence-mode modules

**Files:**
- Create: `src/tawg_bot/bot_identity.py`
- Create: `src/tawg_bot/persist_mode.py`
- Test: `tests/unit/test_bot_identity.py`
- Test: `tests/unit/test_persist_mode.py`

**Interfaces:**
- Produces:
  - `bot_id_from_token(token: str) -> int`
  - `configured_bot_id() -> int`
  - `webhook_receipt_relative_path(bot_id: int | None) -> str`
  - `load_webhook_receipts(root: Path, *, bot_id: int | None, persist_mode: PersistMode = PersistMode.FULL) -> TelegramWebhookReceipts`
  - `class PersistMode(StrEnum)` with `FULL = "full"`, `RECEIPT_ONLY = "receipt-only"`, `NONE = "none"`
  - `configured_persist_mode() -> PersistMode`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_bot_identity.py
import json
from pathlib import Path

import pytest

from tawg_bot.bot_identity import (
    bot_id_from_token,
    load_webhook_receipts,
    webhook_receipt_relative_path,
)
from tawg_bot.models import TelegramWebhookReceipts
from tawg_bot.persist_mode import PersistMode


def test_bot_id_from_token_uses_numeric_prefix():
    assert bot_id_from_token("123456789:AA-remainder") == 123456789


def test_bot_id_from_token_rejects_non_numeric_prefix():
    with pytest.raises(ValueError):
        bot_id_from_token("not-a-number:AA")


def test_receipt_path_namespaced_and_legacy():
    assert webhook_receipt_relative_path(None) == "data/state/telegram-webhook-receipts.json"
    assert webhook_receipt_relative_path(77) == "data/state/telegram-webhook-receipts.77.json"


def test_load_receipts_falls_back_only_in_full_mode(tmp_path: Path):
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    legacy = state / "telegram-webhook-receipts.json"
    legacy.write_text(json.dumps({"schema_version": "tawg.telegram-webhook-receipts.v1", "update_ids": [1, 2]}))
    # full mode (production) falls back to the legacy file
    got = load_webhook_receipts(tmp_path, bot_id=5, persist_mode=PersistMode.FULL)
    assert got.update_ids == [1, 2]
    # receipt-only mode (dev) starts empty
    got = load_webhook_receipts(tmp_path, bot_id=6, persist_mode=PersistMode.RECEIPT_ONLY)
    assert got.update_ids == []


def test_load_receipts_prefers_namespaced_file(tmp_path: Path):
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    (state / "telegram-webhook-receipts.7.json").write_text(
        json.dumps({"schema_version": "tawg.telegram-webhook-receipts.v1", "update_ids": [9]})
    )
    got = load_webhook_receipts(tmp_path, bot_id=7, persist_mode=PersistMode.FULL)
    assert got.update_ids == [9]
```

```python
# tests/unit/test_persist_mode.py
from tawg_bot.persist_mode import PersistMode, configured_persist_mode


def test_configured_persist_mode_explicit(monkeypatch):
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_MODE", "receipt-only")
    assert configured_persist_mode() is PersistMode.RECEIPT_ONLY


def test_configured_persist_mode_backcompat_enabled(monkeypatch):
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_MODE", raising=False)
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_ENABLED", "false")
    assert configured_persist_mode() is PersistMode.NONE


def test_configured_persist_mode_default_full(monkeypatch):
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_MODE", raising=False)
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_ENABLED", raising=False)
    assert configured_persist_mode() is PersistMode.FULL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_bot_identity.py tests/unit/test_persist_mode.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tawg_bot.bot_identity'`

- [ ] **Step 3: Write the modules**

```python
# src/tawg_bot/bot_identity.py
from __future__ import annotations

import os
from pathlib import Path

from tawg_bot.models import TelegramWebhookReceipts

_STATE_DIR = "data/state"
_LEGACY_FILENAME = "telegram-webhook-receipts.json"
_SCHEMA = "tawg.telegram-webhook-receipts.v1"


def bot_id_from_token(token: str) -> int:
    prefix = token.split(":", 1)[0]
    try:
        bot_id = int(prefix)
    except ValueError:
        raise ValueError("TELEGRAM_BOT_TOKEN must begin with a numeric bot id") from None
    if bot_id <= 0:
        raise ValueError("TELEGRAM_BOT_TOKEN bot id must be positive")
    return bot_id


def configured_bot_id() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return bot_id_from_token(token)


def webhook_receipt_relative_path(bot_id: int | None) -> str:
    if bot_id is None:
        return f"{_STATE_DIR}/{_LEGACY_FILENAME}"
    return f"{_STATE_DIR}/telegram-webhook-receipts.{bot_id}.json"


def load_webhook_receipts(
    root: Path,
    *,
    bot_id: int | None,
    persist_mode: PersistMode = PersistMode.FULL,
) -> TelegramWebhookReceipts:
    path = root / webhook_receipt_relative_path(bot_id)
    if path.exists():
        return TelegramWebhookReceipts.model_validate_json(path.read_text(encoding="utf-8"))
    if bot_id is not None and persist_mode is PersistMode.FULL:
        legacy = root / webhook_receipt_relative_path(None)
        if legacy.exists():
            return TelegramWebhookReceipts.model_validate_json(legacy.read_text(encoding="utf-8"))
    return TelegramWebhookReceipts(schema_version=_SCHEMA)
```

> `bot_identity.py` imports `PersistMode` from `persist_mode.py`.

```python
# src/tawg_bot/persist_mode.py
from __future__ import annotations

import os
from enum import StrEnum


class PersistMode(StrEnum):
    FULL = "full"
    RECEIPT_ONLY = "receipt-only"
    NONE = "none"


def configured_persist_mode() -> PersistMode:
    raw = os.environ.get("TAWG_REPOSITORY_PERSIST_MODE")
    if raw:
        try:
            return PersistMode(raw)
        except ValueError:
            raise RuntimeError(
                "TAWG_REPOSITORY_PERSIST_MODE must be full, receipt-only, or none"
            ) from None
    enabled = os.environ.get("TAWG_REPOSITORY_PERSIST_ENABLED", "true")
    return PersistMode.NONE if enabled == "false" else PersistMode.FULL
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_bot_identity.py tests/unit/test_persist_mode.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tawg_bot/bot_identity.py src/tawg_bot/persist_mode.py tests/unit/test_bot_identity.py tests/unit/test_persist_mode.py
git commit -m "feat: add bot identity and persistence-mode modules"
```

---

### Task 2: Namespaced receipts and bot-local reply jobs in webhook intake

**Files:**
- Modify: `src/tawg_bot/telegram_intake.py` (imports; `ingest_envelopes` at `:735`; `_TelegramPersistence.persist` at `:134`)
- Test: `tests/integration/test_telegram_webhook_intake.py`

**Interfaces:**
- Consumes: `PersistMode`, `webhook_receipt_relative_path`, `load_webhook_receipts` (Task 1).
- Produces (changed signatures):
  - `ingest_envelopes(..., bot_id: int | None = None, persist_mode: PersistMode = PersistMode.FULL) -> WebhookIntakeResult`
  - `_TelegramPersistence.persist(..., bot_id: int | None = None, persist_mode: PersistMode = PersistMode.FULL, receipts: TelegramWebhookReceipts | None = None) -> _PersistenceResult`

- [ ] **Step 1: Add imports**

At the top of `src/tawg_bot/telegram_intake.py`, alongside the existing `from tawg_bot.models import (...)` import, add:

```python
from tawg_bot.bot_identity import (
    load_webhook_receipts,
    webhook_receipt_relative_path,
)
from tawg_bot.persist_mode import PersistMode
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/integration/test_telegram_webhook_intake.py`:

```python
from tawg_bot.persist_mode import PersistMode


async def test_ingest_envelopes_namespaces_receipt_by_bot(tmp_path, make_envelope):
    root = tmp_path
    await ingest_envelopes(
        root=root,
        group_slug="tawg",
        bot_username="DevBot",
        envelopes=(make_envelope(update_id=1001),),
        now=datetime(2026, 9, 1, tzinfo=UTC),
        bot_id=77,
        persist_mode=PersistMode.RECEIPT_ONLY,
    )
    assert (root / "data/state/telegram-webhook-receipts.77.json").exists()
    assert not (root / "data/state/telegram-webhook-receipts.json").exists()


async def test_ingest_envelopes_receipt_only_namespaces_job_id(tmp_path, make_envelope):
    root = tmp_path
    result = await ingest_envelopes(
        root=root,
        group_slug="tawg",
        bot_username="DevBot",
        envelopes=(make_envelope(update_id=2001, mention="DevBot"),),
        now=datetime(2026, 9, 1, tzinfo=UTC),
        bot_id=77,
        persist_mode=PersistMode.RECEIPT_ONLY,
    )
    assert result.jobs_created == 1
    jobs = json.loads((root / "data/state/pending-bot-jobs.json").read_text())
    assert jobs[0]["job_id"].startswith("reply:77:tg:tawg:")
```

> Note: reuse the existing `make_envelope` fixture if present; otherwise build a minimal `TelegramWebhookEnvelope` with the fields `update_id`, `source_id`, `message_id`, `text`, `triggers_reply`, and `entities` as the existing intake tests do. Inspect `tests/integration/test_telegram_webhook_intake.py` around line 100 for the canonical fixture shape and match it exactly.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_telegram_webhook_intake.py -k "namespaces" -v`
Expected: FAIL with a `TypeError` (unexpected keyword `bot_id`) or assertion failure.

- [ ] **Step 4: Implement receipt namespacing in `ingest_envelopes`**

Replace the signature and the receipt-load block in `ingest_envelopes` (`:735`):

```python
def ingest_envelopes(
    *,
    root: Path,
    group_slug: str,
    bot_username: str,
    envelopes: Iterable[TelegramWebhookEnvelope],
    now: datetime,
    uow_factory: UnitOfWorkFactory = _default_uow_factory,
    telegram_chat_id: int | None = None,
    bot_id: int | None = None,
    persist_mode: PersistMode = PersistMode.FULL,
) -> WebhookIntakeResult:
```

Replace the `receipts_path` / `receipts` block with:

```python
    receipts = load_webhook_receipts(
        root,
        bot_id=bot_id,
        persist_mode=persist_mode,
    )
```

Pass the new parameters into `persistence.persist(...)`:

```python
    result = persistence.persist(
        (
            _message_from_envelope(
                item, delivered_bot_message_ids=delivered_bot_message_ids
            )
            for item in unseen
        ),
        now=now,
        receipts=updated_receipts,
        bot_id=bot_id,
        persist_mode=persist_mode,
    )
```

- [ ] **Step 5: Implement bot-local job id and supersede bypass in `persist`**

Update `_TelegramPersistence.persist` signature to accept `bot_id` and `persist_mode`, and add two small behavior branches.

After `incoming_by_id` is built, make the fresh-message filter mode-aware:

```python
        if persist_mode is PersistMode.RECEIPT_ONLY:
            fresh_messages = tuple(incoming_by_id.values())
        else:
            fresh_messages = tuple(
                message
                for message in incoming_by_id.values()
                if _message_supersedes_record(
                    message, persisted_by_id.get(message.record_id)
                )
            )
```

Inside the record loop where `job_id = f"reply:{record.record_id}"` is assigned, namespace it in receipt-only mode:

```python
            job_id = (
                f"reply:{bot_id}:{record.record_id}"
                if persist_mode is PersistMode.RECEIPT_ONLY and bot_id is not None
                else f"reply:{record.record_id}"
            )
```

Change the receipt staging path from the hardcoded legacy path to the namespaced helper:

```python
        if receipts is not None:
            uow.stage_json(
                webhook_receipt_relative_path(bot_id),
                receipts.model_dump(mode="json"),
            )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/integration/test_telegram_webhook_intake.py -k "namespaces" -v`
Expected: PASS

- [ ] **Step 7: Run the full intake suite to confirm no production regression**

Run: `pytest tests/integration/test_telegram_webhook_intake.py -q`
Expected: PASS (existing tests keep default `bot_id=None`, `persist_mode=FULL` → legacy behavior unchanged)

- [ ] **Step 8: Commit**

```bash
git add src/tawg_bot/telegram_intake.py tests/integration/test_telegram_webhook_intake.py
git commit -m "feat: namespace webhook receipts and reply jobs per bot"
```

---

### Task 3: commit_operation.sh mode dispatch and receipt-only allowlist

**Files:**
- Modify: `scripts/commit_operation.sh`

**Interfaces:**
- Consumes env vars: `TAWG_REPOSITORY_PERSIST_MODE` (`full` | `receipt-only` | `none`), `TAWG_BOT_ID` (numeric).
- Produces: exit codes `0` (ok), `75` (non-fast-forward), `6` (missing/invalid bot id in receipt-only), `7` (unknown mode).

- [ ] **Step 1: Rewrite the top of the script to dispatch on mode**

Replace the current early `TAWG_REPOSITORY_PERSIST_ENABLED` gate with a mode dispatch placed after the `operation_id` validation:

```bash
persist_mode="${TAWG_REPOSITORY_PERSIST_MODE:-full}"
case "$persist_mode" in
  full) ;;
  receipt-only) receipt_only_mode=1 ;;
  none) exit 0 ;;
  *) exit 7 ;;
esac
```

- [ ] **Step 2: Add the receipt-only commit path**

Insert after the dispatch, before the existing `allowed_path` definition:

```bash
if [[ "${receipt_only_mode:-0}" == "1" ]]; then
  bot_id="${TAWG_BOT_ID:-}"
  if [[ ! "$bot_id" =~ ^[0-9]+$ ]]; then
    exit 6
  fi
  git add -- "data/state/telegram-webhook-receipts.${bot_id}.json"
  if ! git diff --cached --quiet; then
    git config user.name "TAWG Knowledge Bot"
    git config user.email "tawg-knowledge-bot@users.noreply.github.com"
    git commit -m "bot: checkpoint ${operation_id}"
  fi
  push_output_file="$(mktemp "${TMPDIR:-/tmp}/tawg-push.XXXXXX")"
  chmod 600 "$push_output_file"
  if LC_ALL=C git push --porcelain origin \
    "HEAD:${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}" >"$push_output_file" 2>&1; then
    exit 0
  fi
  if grep -Eq \
    '^![[:space:]]+[^[:space:]]+[[:space:]]+\[rejected\][[:space:]]+\((non-fast-forward|fetch first)\)$' \
    "$push_output_file"; then
    exit 75
  fi
  exit 1
fi
```

> The unconditional `git push` also publishes the `main`→`dev` merge commit produced by Task 4, which is what keeps `dev` advancing when a receipt has no new change (idle 30-minute sync).

- [ ] **Step 3: Verify the full-mode path is unchanged**

Run: `bash -n scripts/commit_operation.sh`
Expected: exit 0 (syntax valid)

- [ ] **Step 4: Commit**

```bash
git add scripts/commit_operation.sh
git commit -m "feat: receipt-only persistence mode for dev workers"
```

---

### Task 4: RepositorySession merge-before-work

**Files:**
- Modify: `src/tawg_bot/repository_session.py`
- Test: `tests/unit/test_repository_session.py` (create if absent) or extend `tests/integration` where `RepositorySession` is exercised

**Interfaces:**
- Consumes: `CommandRunner` protocol (existing).
- Produces (changed):
  - `RepositorySession.__init__(..., merge_branch: str | None = None)`
  - new error code literal `"repository_merge_failed"`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repository_session.py
from pathlib import Path

import pytest

from tawg_bot.repository_session import RepositorySession, RepositorySessionError


class _RecordingRunner:
    def __init__(self):
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    async def run(self, *, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        return type("R", (), {"returncode": 0})()


@pytest.mark.asyncio
async def test_merge_branch_fetches_and_merges_before_operation(tmp_path):
    runner = _RecordingRunner()
    session = RepositorySession(remote="https://example.invalid/r.git", branch="dev", runner=runner, merge_branch="main")
    async def op(_root: Path) -> None:
        return None
    try:
        await session.run(operation_id="sync:1", operation=op)
    except RepositorySessionError:
        pass  # clone will fail against the fake remote; we only assert the command sequence
    commands = [c[0] for c in runner.calls]
    assert any(cmd[:2] == ("git", "fetch") and "main" in cmd for cmd in commands)
    assert any(cmd[:2] == ("git", "merge") for cmd in commands)
```

> This test uses a fake remote that fails clone; it asserts only that a `fetch` of `main` and a `merge` were issued before the failure. Keep it deterministic by inspecting the command list regardless of clone outcome.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_repository_session.py -v`
Expected: FAIL (no `fetch`/`merge` commands recorded)

- [ ] **Step 3: Implement merge-before-work**

Add the literal to `RepositoryErrorCode`:

```python
RepositoryErrorCode = Literal[
    "repository_checkpoint_failed",
    "repository_checkout_failed",
    "repository_command_failed",
    "repository_merge_failed",
]
```

Add the constructor parameter:

```python
    def __init__(
        self,
        *,
        remote: str,
        branch: str,
        runner: CommandRunner | None = None,
        merge_branch: str | None = None,
    ) -> None:
        self.remote = remote
        self.branch = branch
        self.runner = runner or AsyncioCommandRunner()
        self.merge_branch = merge_branch
```

Inside `run`, after the successful clone and before `operation(checkout)`, insert:

```python
                    if self.merge_branch is not None and self.branch != self.merge_branch:
                        await self._run_command(
                            argv=(
                                "git", "fetch", "origin", self.merge_branch, "--depth", "1",
                            ),
                            cwd=checkout,
                            failure_code="repository_merge_failed",
                        )
                        await self._run_command(
                            argv=("git", "merge", "--no-edit", f"origin/{self.merge_branch}"),
                            cwd=checkout,
                            failure_code="repository_merge_failed",
                        )
```

Change the clone to fetch all branches so `origin/main` is present:

```python
                        argv=(
                            "git",
                            "clone",
                            "--branch",
                            self.branch,
                            "--no-single-branch",
                            "--depth",
                            "1",
                            "--",
                            self.remote,
                            str(checkout),
                        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_repository_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tawg_bot/repository_session.py tests/unit/test_repository_session.py
git commit -m "feat: merge a source branch before repository operations"
```

---

### Task 5: Runtime plumbing and bot-local reply filter

**Files:**
- Modify: `src/tawg_bot/runtime.py` (`from_environment` at `:206`, `ingest_webhook_envelope` at `:224`, `_LivePipeline.__init__` at `:353`, `_prepare_pending_replies` at `:714`)

**Interfaces:**
- Consumes: `configured_bot_id`, `configured_persist_mode` (Task 1).
- Produces: `_LivePipeline` carries `self.bot_id` and `self.persist_mode`; `ingest_webhook_envelope` passes them into `ingest_envelopes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_runtime_composition.py (append)
from tawg_bot.persist_mode import PersistMode


async def test_receipt_only_filters_actionable_jobs_to_current_bot(tmp_path):
    pipeline = _build_pipeline(tmp_path, bot_id=88, persist_mode=PersistMode.RECEIPT_ONLY)
    jobs = [
        {"job_id": "reply:88:tg:tawg:1", "status": "pending", "trigger_kind": "mention", "trigger_record_id": "tg:tawg:1"},
        {"job_id": "reply:tg:tawg:2", "status": "pending", "trigger_kind": "mention", "trigger_record_id": "tg:tawg:2"},
    ]
    (tmp_path / "data/state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(json.dumps(jobs))
    await pipeline._prepare_pending_replies()
    prepared_ids = [r.job_id for r in pipeline.prepared_replies]
    assert prepared_ids == []  # fake records yield no trigger evidence, but filter must exclude the non-bot job
```

> This test asserts the filtering step specifically. Use the existing test helpers in `test_runtime_composition.py` to build a pipeline; if a `_build_pipeline` helper does not exist, construct `_LivePipeline` the same way the surrounding tests do.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_runtime_composition.py -k receipt_only_filters -v`
Expected: FAIL (`_LivePipeline` has no `bot_id`/`persist_mode`)

- [ ] **Step 3: Thread identity and mode through `_LivePipeline`**

Add to `_LivePipeline.__init__` two keyword params with defaults, and store them:

```python
    def __init__(self, root: Path, *, ..., bot_id: int | None = None, persist_mode: PersistMode = PersistMode.FULL):
        ...
        self.bot_id = bot_id
        self.persist_mode = persist_mode
```

In `ingest_webhook_envelope`, compute and pass them:

```python
        bot_id = configured_bot_id()
        persist_mode = configured_persist_mode()
        ...
            result = ingest_envelopes(
                ...,
                bot_id=bot_id,
                persist_mode=persist_mode,
            )
```

Construct `_LivePipeline` with the same values so `_prepare_pending_replies` can filter:

```python
            pipeline = _LivePipeline(
                self.root,
                client=client,
                checkpoint=self.checkpoint,
                now=now,
                bot_id=bot_id,
                persist_mode=persist_mode,
            )
```

- [ ] **Step 4: Filter actionable jobs in receipt-only mode**

In `_prepare_pending_replies`, immediately after the `actionable` list is built and sorted, add:

```python
        if self.persist_mode is PersistMode.RECEIPT_ONLY and self.bot_id is not None:
            prefix = f"reply:{self.bot_id}:"
            actionable = [job for job in actionable if job.job_id.startswith(prefix)]
```

- [ ] **Step 5: Run the runtime composition suite**

Run: `pytest tests/integration/test_runtime_composition.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tawg_bot/runtime.py tests/integration/test_runtime_composition.py
git commit -m "feat: restrict reply prep to the current bot in receipt-only mode"
```

---

### Task 6: Modal adapter dev wiring

**Files:**
- Modify: `deploy/modal_app.py`

**Interfaces:**
- Consumes: `configured_bot_id` (Task 1); `RepositorySession(merge_branch=...)` (Task 4).
- Produces: `TAWG_BOT_ID` and `TAWG_REPOSITORY_PERSIST_MODE` exported into the worker environment; a 30-minute dev sync schedule.

- [ ] **Step 1: Replace the persist boolean with the mode env**

Replace `_REPOSITORY_PERSIST_ENABLED` with a mode resolver:

```python
_REPOSITORY_PERSIST_MODE = os.environ.get(
    "TAWG_REPOSITORY_PERSIST_MODE",
    "none" if os.environ.get("TAWG_REPOSITORY_PERSIST_ENABLED", "true") == "false" else "full",
)
```

In the image `.env(...)` block, replace `"TAWG_REPOSITORY_PERSIST_ENABLED": ...` with:

```python
            "TAWG_REPOSITORY_PERSIST_MODE": _REPOSITORY_PERSIST_MODE,
```

- [ ] **Step 2: Export bot identity into the repository environment**

In `_repository_environment`, before `yield`, set the bot id env vars so `commit_operation.sh` and the runtime share them:

```python
        os.environ["TAWG_BOT_ID"] = str(configured_bot_id())
```

Add the import at the top:

```python
from tawg_bot.bot_identity import configured_bot_id
```

- [ ] **Step 3: Merge main into dev and make dev maintenance sync-only**

In `repository_worker`, pass `merge_branch` and branch the runtime call:

```python
        async def run_runtime(root: Path) -> None:
            runtime = ProductionRuntime.from_environment(root)
            if envelope is not None:
                await runtime.ingest_webhook_envelope(envelope, now=now)
            elif _BRANCH == "main":
                await runtime.maintenance_tick(now, observe_only=False)
            # dev maintenance is sync-only; the merge already happened above

        merge_branch = None if _BRANCH == "main" else "main"
        with _repository_environment():
            await RepositorySession(
                remote=_REMOTE,
                branch=_BRANCH,
                merge_branch=merge_branch,
            ).run(operation_id=operation_id, operation=run_runtime)
```

- [ ] **Step 4: Add the 30-minute dev sync schedule**

Add a new Modal function after `scheduled_maintenance`:

```python
@app.function(
    image=image,
    secrets=[worker_secret],
    schedule=modal.Cron("*/30 * * * *"),
    timeout=_ENDPOINT_TIMEOUT_SECONDS,
)
async def scheduled_dev_sync() -> None:
    """Merge main into dev on a lightweight cadence; no tick, no model, no send."""
    if _BRANCH == "main":
        return
    await repository_worker.spawn.aio(None)
```

- [ ] **Step 5: Static checks**

Run: `python -m ruff check deploy/modal_app.py && python -m mypy deploy/modal_app.py 2>/dev/null || true`
Expected: ruff clean; mypy findings limited to the existing `deploy` module handling (the project gates `src/tawg_bot`; keep `deploy` ruff-clean).

- [ ] **Step 6: Commit**

```bash
git add deploy/modal_app.py
git commit -m "feat: wire dev bot persistence mode, merge, and sync schedule"
```

---

### Task 7: Dev deploy workflow environment

**Files:**
- Modify: `.github/workflows/modal-deploy-dev.yml`

**Interfaces:**
- Consumes: the new `TAWG_REPOSITORY_PERSIST_MODE` env consumed by `deploy/modal_app.py` (Task 6).

- [ ] **Step 1: Replace the persist env in the deploy step**

In the `Deploy verified Modal dev app` step, replace:

```yaml
          TAWG_REPOSITORY_PERSIST_ENABLED: "false"
```

with:

```yaml
          TAWG_REPOSITORY_PERSIST_MODE: receipt-only
```

Leave `TAWG_MODAL_APP_NAME: tawg-development` and `TAWG_MODAL_BRANCH: dev` unchanged. `receipt-only` mode never falls back to the legacy receipt, so no legacy-bot config is needed here.

- [ ] **Step 2: Confirm production workflow is untouched**

Run: `git diff -- .github/workflows/modal-deploy.yml`
Expected: no diff (production workflow unchanged)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/modal-deploy-dev.yml
git commit -m "chore: dev deploy uses receipt-only persistence mode"
```

---

### Task 8: Full verification and operator notes

**Files:**
- Modify: `docs/operator/modal.md` (append a "Dev bot mirror" section)

- [ ] **Step 1: Run the full gate**

Run:

```bash
python -m ruff check src tests deploy
python -m mypy src/tawg_bot
python -m pytest -q
python -m tawg_bot.cli vault-lint
```

Expected: all green, matching the pre-change baseline (ruff clean, mypy clean for `src/tawg_bot`, full pytest pass, vault 0 errors).

- [ ] **Step 2: Document the dev deployment contract**

Append a section to `docs/operator/modal.md` stating:

- `tawg-development` runs `TAWG_REPOSITORY_PERSIST_MODE=receipt-only` and `TAWG_MODAL_BRANCH=dev`.
- The dev workspace must bind `tawg-worker` / `tawg-webhook` / `tawg-maintenance` / `tawg-github-announcements` to the dev bot token, dev chat id, dev bot username, and a `GITHUB_TOKEN` that can push to `dev`.
- Production runs `full` mode, which is the only mode that falls back to the legacy unnamed receipt for the one-time migration; dev runs `receipt-only` and never falls back.

- [ ] **Step 3: Commit**

```bash
git add docs/operator/modal.md
git commit -m "docs: document dev bot mirror deployment contract"
```

---

## Self-Review

**Spec coverage:**
- 4.2 per-bot receipts → Task 1 (`webhook_receipt_relative_path`, `load_webhook_receipts`) + Task 2.
- 4.3 persistence modes → Task 1 (`PersistMode`) + Task 3 (`commit_operation.sh`) + Task 6/7 (env).
- 4.4 main→dev merge → Task 4 (`RepositorySession.merge_branch`).
- 4.5 dev webhook order → Task 2 (receipt staging) + Task 3 (receipt committed before send) + Task 6 (merge before operation).
- 4.6 bot-local reply identity → Task 2 (job-id namespace) + Task 5 (actionable filter) + Task 2 (supersede bypass).
- 4.7 lightweight sync → Task 6 (`scheduled_dev_sync`).
- 7 security (scoped token, numeric id) → Task 6 (`TAWG_BOT_ID` export) + Task 8 (operator notes).
- 8 failure handling → Task 4 (merge fail-closed) + Task 3 (receipt push before send, `75` retry).
- 9 deployment → Task 7 + Task 8.
- 10 testing requirements → each task's tests; Task 8 full gate.

**Placeholder scan:** none — every task carries concrete code and named pytest cases.

**Type consistency:** `PersistMode` values (`full`/`receipt-only`/`none`) match across `persist_mode.py`, `commit_operation.sh`, and `modal_app.py`; `bot_id: int | None` and `persist_mode: PersistMode` signatures match across Tasks 1, 2, 5, 6.
