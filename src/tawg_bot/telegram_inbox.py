"""Bot-isolated durable inbox with immutable enqueue and mutable worker state.

Directory layout per bot (on a Modal Volume or a local directory):

    <root>/<bot_key>/incoming/<update_id>.json  # endpoint only, create-once
    <root>/<bot_key>/state/<update_id>.json     # worker only, mutable
    <root>/<bot_key>/done/<update_id>           # worker-only terminal tombstone

The endpoint must never touch state/done; the worker must never rewrite incoming.
This prevents a duplicate webhook from overwriting entry_decided/ambiguous state or
resurrecting a terminal entry.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from tawg_bot.models import StrictModel
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope

INBOX_DIR_ENV = "TAWG_TELEGRAM_INBOX_DIR"
INBOX_BOT_ENV = "TAWG_TELEGRAM_BOT_USERNAME"
INBOX_RECOVERY_LIMIT = 10
INBOX_RECOVERY_BUDGET_SECONDS = 900
INBOX_RETRY_DELAY_SECONDS = 300
INBOX_RETRY_BACKOFF_SECONDS = 5
INBOX_MAX_ATTEMPTS = 3


class InboxStatus(StrEnum):
    ENQUEUED = "enqueued"
    ENTRY_DECIDED = "entry_decided"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERED = "delivered"
    TERMINAL = "terminal"
    AMBIGUOUS = "ambiguous"
    NEEDS_ATTENTION = "needs_attention"


class InboxEntry(StrictModel):
    schema_version: Literal["tawg.telegram-inbox-entry.v1"] = (
        "tawg.telegram-inbox-entry.v1"
    )
    update_id: int = Field(ge=0)
    envelope: TelegramWebhookEnvelope
    enqueued_at: datetime
    status: InboxStatus = InboxStatus.ENQUEUED
    jobs: tuple[dict[str, Any], ...] = ()
    prepared_replies: tuple[dict[str, Any], ...] = ()
    safe_error_code: str | None = Field(default=None, max_length=64)
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None


class _EnvelopeRecord(StrictModel):
    schema_version: Literal["tawg.telegram-inbox-envelope.v1"] = (
        "tawg.telegram-inbox-envelope.v1"
    )
    update_id: int = Field(ge=0)
    envelope: TelegramWebhookEnvelope
    enqueued_at: datetime


class _StateRecord(StrictModel):
    schema_version: Literal["tawg.telegram-inbox-state.v1"] = (
        "tawg.telegram-inbox-state.v1"
    )
    status: InboxStatus
    jobs: tuple[dict[str, Any], ...] = ()
    prepared_replies: tuple[dict[str, Any], ...] = ()
    safe_error_code: str | None = Field(default=None, max_length=64)
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None


def inbox_root() -> Path | None:
    raw = os.environ.get(INBOX_DIR_ENV)
    return Path(raw) if raw else None


def inbox_bot_key() -> str:
    raw = os.environ.get(INBOX_BOT_ENV, "bot")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "bot"


def _bot_root(root: Path, bot_key: str) -> Path:
    return root / bot_key


def incoming_path(root: Path, bot_key: str, update_id: int) -> Path:
    return _bot_root(root, bot_key) / "incoming" / f"{update_id}.json"


def state_path(root: Path, bot_key: str, update_id: int) -> Path:
    return _bot_root(root, bot_key) / "state" / f"{update_id}.json"


def done_path(root: Path, bot_key: str, update_id: int) -> Path:
    return _bot_root(root, bot_key) / "done" / f"{update_id}"


def _fsync_directory(path: Path) -> None:
    """Best-effort directory flush; Modal Volumes may not support directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish(path: Path, payload: bytes) -> None:
    """Write one complete file under a unique temp name, then publish it.

    Modal Volumes have automatic background commits, per-container snapshots,
    last-write-wins for the same file, and no distributed locking. A reader must
    therefore never observe bytes written directly to the published name. Every
    writer writes and fsyncs a uniquely named temp file first, then replaces the
    final name with one metadata operation. Concurrent publishers of the same
    final path are last-write-wins, but each published version is a complete
    record rather than an interleaved partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _try_load_envelope(
    root: Path, bot_key: str, update_id: int
) -> _EnvelopeRecord | None:
    """Read an incoming record, treating an invalid/partially visible file as absent."""
    try:
        return load_envelope(root, bot_key, update_id)
    except (OSError, UnicodeError, ValueError):
        return None


def load_envelope(root: Path, bot_key: str, update_id: int) -> _EnvelopeRecord | None:
    path = incoming_path(root, bot_key, update_id)
    if not path.is_file():
        return None
    return _EnvelopeRecord.model_validate_json(path.read_text(encoding="utf-8"))


def load_state(root: Path, bot_key: str, update_id: int) -> _StateRecord | None:
    path = state_path(root, bot_key, update_id)
    if not path.is_file():
        return None
    return _StateRecord.model_validate_json(path.read_text(encoding="utf-8"))


def enqueue_envelope(
    envelope: TelegramWebhookEnvelope,
    *,
    now: datetime,
    root: Path | None = None,
    bot_key: str | None = None,
) -> InboxEntry:
    resolved_root = root or inbox_root()
    if resolved_root is None:
        raise RuntimeError("telegram inbox is not configured")
    resolved_key = bot_key or inbox_bot_key()
    if done_path(resolved_root, resolved_key, envelope.update_id).exists():
        return _combined(
            _EnvelopeRecord(
                update_id=envelope.update_id,
                envelope=envelope,
                enqueued_at=now,
            ),
            _StateRecord(status=InboxStatus.TERMINAL, safe_error_code="already_done"),
        )
    record = _EnvelopeRecord(
        update_id=envelope.update_id,
        envelope=envelope,
        enqueued_at=now,
    )
    loaded = _try_load_envelope(resolved_root, resolved_key, envelope.update_id)
    if loaded is None:
        # Do not rely on O_EXCL for cross-container dedupe: Modal Volumes use
        # per-container snapshots and last-write-wins. A duplicate publisher may
        # replace this file, but only with another complete record for the same
        # update; worker state lives in a separate file and is never touched here.
        _publish(
            incoming_path(resolved_root, resolved_key, envelope.update_id),
            record.model_dump_json(indent=2).encode("utf-8") + b"\n",
        )
        loaded = _try_load_envelope(
            resolved_root, resolved_key, envelope.update_id
        )
        if loaded is None:
            raise RuntimeError("telegram inbox envelope publish failed")
    state = load_state(resolved_root, resolved_key, envelope.update_id)
    return _combined(loaded, state or _StateRecord(status=InboxStatus.ENQUEUED))


def save_state(entry: InboxEntry, *, root: Path, bot_key: str) -> None:
    """Worker-only write; never touches the immutable incoming record."""
    state = _StateRecord(
        status=entry.status,
        jobs=entry.jobs,
        prepared_replies=entry.prepared_replies,
        safe_error_code=entry.safe_error_code,
        attempts=entry.attempts,
        next_attempt_at=entry.next_attempt_at,
    )
    _publish(
        state_path(root, bot_key, entry.update_id),
        state.model_dump_json(indent=2).encode("utf-8") + b"\n",
    )


def remove_entry(entry: InboxEntry, *, root: Path, bot_key: str) -> None:
    """Publish the terminal tombstone first, then remove the entry records.

    The tombstone is never deleted, so concurrent duplicate enqueue writes after
    the tombstone cannot resurrect the entry: every reader checks the tombstone
    before exposing incoming/state records.
    """
    _publish(done_path(root, bot_key, entry.update_id), b"")
    incoming_path(root, bot_key, entry.update_id).unlink(missing_ok=True)
    state_path(root, bot_key, entry.update_id).unlink(missing_ok=True)


def cleanup_terminal_orphans(
    *,
    root: Path | None = None,
    bot_key: str | None = None,
) -> int:
    """Remove files left behind when a crash followed the terminal tombstone.

    A done marker is the durable terminal decision, so any remaining incoming or
    state file for that update is stale and safe to unlink. This is idempotent and
    is only invoked by maintenance cleanup, never on the entry-decision path.
    """
    resolved_root = root or inbox_root()
    if resolved_root is None:
        return 0
    resolved_key = bot_key or inbox_bot_key()
    bot_directory = _bot_root(resolved_root, resolved_key)
    cleaned: set[int] = set()
    for directory in (bot_directory / "incoming", bot_directory / "state"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                update_id = int(path.stem)
            except ValueError:
                continue
            if not done_path(resolved_root, resolved_key, update_id).exists():
                continue
            path.unlink(missing_ok=True)
            cleaned.add(update_id)
    return len(cleaned)


def entries(
    *,
    root: Path | None = None,
    bot_key: str | None = None,
    statuses: Iterable[InboxStatus] | None = None,
) -> tuple[InboxEntry, ...]:
    resolved_root = root or inbox_root()
    if resolved_root is None:
        return ()
    resolved_key = bot_key or inbox_bot_key()
    directory = _bot_root(resolved_root, resolved_key) / "incoming"
    if not directory.is_dir():
        return ()
    allowed = set(statuses) if statuses is not None else None
    loaded: list[InboxEntry] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            update_id = int(path.stem)
        except ValueError:
            continue
        if done_path(resolved_root, resolved_key, update_id).exists():
            continue
        envelope = _EnvelopeRecord.model_validate_json(path.read_text(encoding="utf-8"))
        state = load_state(resolved_root, resolved_key, update_id)
        entry = _combined(envelope, state or _StateRecord(status=InboxStatus.ENQUEUED))
        if allowed is None or entry.status in allowed:
            loaded.append(entry)
    loaded.sort(key=lambda entry: (entry.enqueued_at, entry.update_id))
    return tuple(loaded)


def get_entry(*, root: Path, bot_key: str, update_id: int) -> InboxEntry | None:
    """Read the latest durable state for one update (never the caller's stale copy)."""
    if done_path(root, bot_key, update_id).exists():
        return None
    envelope = load_envelope(root, bot_key, update_id)
    if envelope is None:
        return None
    state = load_state(root, bot_key, update_id)
    return _combined(envelope, state or _StateRecord(status=InboxStatus.ENQUEUED))


def oldest_undecided_age_seconds(
    *,
    now: datetime,
    root: Path | None = None,
    bot_key: str | None = None,
) -> int:
    pending = entries(
        root=root,
        bot_key=bot_key,
        statuses={InboxStatus.ENQUEUED},
    )
    if not pending:
        return 0
    oldest = min(entry.enqueued_at for entry in pending)
    return max(0, int((now - oldest).total_seconds()))


def _combined(record: _EnvelopeRecord, state: _StateRecord) -> InboxEntry:
    next_attempt_at = state.next_attempt_at
    if state.status is InboxStatus.ENQUEUED and next_attempt_at is None:
        next_attempt_at = record.enqueued_at + timedelta(
            seconds=INBOX_RETRY_DELAY_SECONDS
        )
    return InboxEntry(
        update_id=record.update_id,
        envelope=record.envelope,
        enqueued_at=record.enqueued_at,
        status=state.status,
        jobs=state.jobs,
        prepared_replies=state.prepared_replies,
        safe_error_code=state.safe_error_code,
        attempts=state.attempts,
        next_attempt_at=next_attempt_at,
    )


@dataclass(frozen=True, slots=True)
class InboxDrainSelection:
    selected: tuple[InboxEntry, ...]
    new_event_updates: tuple[int, ...]
    backlog_seen: int
    backlog_selected: int
    backlog_remaining: int
    oldest_undecided_age_seconds: int
    maintenance_only: bool
    retry_selected: int
    needs_attention: int


def select_drain_entries(
    all_entries: Sequence[InboxEntry],
    *,
    new_update_ids: frozenset[int] | None,
    now: datetime,
    recovery_limit: int = INBOX_RECOVERY_LIMIT,
    delivery_limit: int = 10,
) -> InboxDrainSelection:
    """Select work without treating an empty tick or an undecided initial event as a retry."""
    enqueued = sorted(
        (entry for entry in all_entries if entry.status is InboxStatus.ENQUEUED),
        key=lambda entry: (entry.enqueued_at, entry.update_id),
    )
    delivery = [
        entry
        for entry in all_entries
        if entry.status in {InboxStatus.DELIVERY_PENDING, InboxStatus.ENTRY_DECIDED}
    ]
    terminal = [
        entry
        for entry in all_entries
        if entry.status in {InboxStatus.DELIVERED, InboxStatus.TERMINAL}
    ]
    due_retries = [
        entry
        for entry in enqueued
        if entry.next_attempt_at is not None
        and entry.next_attempt_at <= now
        and entry.attempts < INBOX_MAX_ATTEMPTS
    ]
    needs_attention = sum(
        1 for entry in all_entries if entry.status is InboxStatus.NEEDS_ATTENTION
    )
    oldest = min((entry.enqueued_at for entry in enqueued), default=None)
    oldest_age = 0 if oldest is None else max(0, int((now - oldest).total_seconds()))

    if new_update_ids is None:
        # Maintenance may resume only already-decided/delivery work and finish
        # terminal cleanup. It must never run an entry decision for a queued trigger.
        selected = [*terminal[:delivery_limit], *delivery[:delivery_limit]]
        return InboxDrainSelection(
            selected=tuple(selected),
            new_event_updates=(),
            backlog_seen=len(enqueued),
            backlog_selected=0,
            backlog_remaining=len(enqueued),
            oldest_undecided_age_seconds=oldest_age,
            maintenance_only=True,
            retry_selected=0,
            needs_attention=needs_attention,
        )

    new_events = tuple(entry for entry in enqueued if entry.update_id in new_update_ids)
    new_ids = {entry.update_id for entry in new_events}
    retry_ids = {entry.update_id for entry in due_retries}
    retry_entries = [
        entry for entry in due_retries if entry.update_id not in new_ids
    ]
    backlog = [
        entry
        for entry in enqueued
        if entry.update_id not in new_ids and entry.update_id not in retry_ids
    ]
    selected_backlog = backlog[:recovery_limit]
    candidates = [
        *new_events,
        *terminal[:delivery_limit],
        *delivery[:delivery_limit],
        *retry_entries[:recovery_limit],
        *selected_backlog,
    ]
    seen: set[int] = set()
    deduped: list[InboxEntry] = []
    for entry in candidates:
        if entry.update_id in seen:
            continue
        seen.add(entry.update_id)
        deduped.append(entry)
    selected_enqueued = [
        entry
        for entry in deduped
        if entry.status is InboxStatus.ENQUEUED and entry.update_id not in new_ids
    ]
    return InboxDrainSelection(
        selected=tuple(deduped),
        new_event_updates=tuple(entry.update_id for entry in new_events),
        backlog_seen=len(enqueued),
        backlog_selected=len(selected_enqueued),
        backlog_remaining=len(enqueued) - len(new_events) - len(selected_enqueued),
        oldest_undecided_age_seconds=oldest_age,
        maintenance_only=False,
        retry_selected=len(retry_entries),
        needs_attention=needs_attention,
    )
