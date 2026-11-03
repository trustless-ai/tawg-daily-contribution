from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import tawg_bot.telegram_inbox as inbox_module
from tawg_bot.telegram_inbox import (
    INBOX_MAX_ATTEMPTS,
    INBOX_RETRY_DELAY_SECONDS,
    InboxStatus,
    _publish,
    cleanup_terminal_orphans,
    done_path,
    enqueue_envelope,
    entries,
    incoming_path,
    load_state,
    remove_entry,
    save_state,
    select_drain_entries,
    state_path,
)
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope

NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
BOT = "tawg_dev_bot"


def _envelope(update_id: int, message_id: int, text: str) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=False,
        text=text,
        display_name="Alice",
        triggers_reply=False,
        integrity_digest="0" * 64,
    )
    payload = value.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value.model_copy(update={"integrity_digest": digest})


def test_duplicate_enqueue_does_not_overwrite_worker_state(tmp_path: Path) -> None:
    envelope = _envelope(1, 10, "hello")
    first = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    decided = first.model_copy(
        update={
            "status": InboxStatus.ENTRY_DECIDED,
            "jobs": ({"job_id": "reply:tg:tawg:10"},),
            "attempts": 1,
        }
    )
    save_state(decided, root=tmp_path, bot_key=BOT)

    # A duplicate webhook must not clobber the worker's entry_decided state.
    duplicate = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    assert duplicate.status is InboxStatus.ENTRY_DECIDED
    assert duplicate.jobs == ({"job_id": "reply:tg:tawg:10"},)
    assert duplicate.attempts == 1
    assert [entry.status for entry in entries(root=tmp_path, bot_key=BOT)] == [
        InboxStatus.ENTRY_DECIDED
    ]


def test_duplicate_enqueue_does_not_overwrite_ambiguous_state(tmp_path: Path) -> None:
    envelope = _envelope(2, 11, "ambiguous case")
    entry = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    ambiguous = entry.model_copy(
        update={
            "status": InboxStatus.AMBIGUOUS,
            "prepared_replies": ({"job_id": "reply:tg:tawg:11"},),
        }
    )
    save_state(ambiguous, root=tmp_path, bot_key=BOT)

    duplicate = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    assert duplicate.status is InboxStatus.AMBIGUOUS
    assert [entry.status for entry in entries(root=tmp_path, bot_key=BOT)] == [
        InboxStatus.AMBIGUOUS
    ]


def test_terminal_tombstone_prevents_resurrection_by_duplicate_webhook(
    tmp_path: Path,
) -> None:
    envelope = _envelope(3, 12, "already delivered")
    entry = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    delivered = entry.model_copy(update={"status": InboxStatus.DELIVERED})
    save_state(delivered, root=tmp_path, bot_key=BOT)
    remove_entry(delivered, root=tmp_path, bot_key=BOT)

    assert done_path(tmp_path, BOT, 3).exists()
    assert not incoming_path(tmp_path, BOT, 3).exists()
    assert not state_path(tmp_path, BOT, 3).exists()

    duplicate = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    assert duplicate.status is InboxStatus.TERMINAL
    assert duplicate.safe_error_code == "already_done"
    assert entries(root=tmp_path, bot_key=BOT) == ()
    assert not incoming_path(tmp_path, BOT, 3).exists()


def test_different_updates_coexist_and_are_ordered(tmp_path: Path) -> None:
    later = _envelope(20, 22, "later")
    earlier = _envelope(10, 21, "earlier")
    enqueue_envelope(later, now=NOW, root=tmp_path, bot_key=BOT)
    enqueue_envelope(earlier, now=NOW, root=tmp_path, bot_key=BOT)

    pending = entries(root=tmp_path, bot_key=BOT)
    assert [entry.update_id for entry in pending] == [10, 20]
    assert all(entry.status is InboxStatus.ENQUEUED for entry in pending)


def test_worker_state_and_incoming_are_separate_files(tmp_path: Path) -> None:
    envelope = _envelope(30, 31, "separate")
    entry = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    incoming_before = incoming_path(tmp_path, BOT, 30).read_bytes()
    decided = entry.model_copy(
        update={
            "status": InboxStatus.DELIVERY_PENDING,
            "prepared_replies": ({"job_id": "reply:tg:tawg:31"},),
        }
    )
    save_state(decided, root=tmp_path, bot_key=BOT)

    assert incoming_path(tmp_path, BOT, 30).read_bytes() == incoming_before
    state = load_state(tmp_path, BOT, 30)
    assert state is not None
    assert state.status is InboxStatus.DELIVERY_PENDING
    assert entries(root=tmp_path, bot_key=BOT)[0].prepared_replies == (
        {"job_id": "reply:tg:tawg:31"},
    )


def _seed_entry(
    root: Path,
    update_id: int,
    status: InboxStatus,
) -> None:
    envelope = _envelope(update_id, 1000 + update_id, f"message {update_id}")
    entry = enqueue_envelope(envelope, now=NOW, root=root, bot_key=BOT)
    if status is not InboxStatus.ENQUEUED:
        save_state(entry.model_copy(update={"status": status}), root=root, bot_key=BOT)


def test_maintenance_selects_delivery_but_never_enqueued(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 1, InboxStatus.ENQUEUED)
    _seed_entry(tmp_path, 2, InboxStatus.ENQUEUED)
    _seed_entry(tmp_path, 3, InboxStatus.DELIVERY_PENDING)
    _seed_entry(tmp_path, 4, InboxStatus.AMBIGUOUS)

    selection = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW,
    )

    assert [entry.update_id for entry in selection.selected] == [3]
    assert selection.maintenance_only is True
    assert selection.backlog_seen == 2
    assert selection.backlog_remaining == 2
    assert selection.new_event_updates == ()


def test_real_event_recovers_at_most_ten_backlog_and_preserves_the_rest(
    tmp_path: Path,
) -> None:
    for update_id in range(10, 25):
        _seed_entry(tmp_path, update_id, InboxStatus.ENQUEUED)
    _seed_entry(tmp_path, 100, InboxStatus.ENQUEUED)

    selection = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=frozenset({100}),
        now=NOW,
    )

    assert selection.new_event_updates == (100,)
    assert selection.backlog_seen == 16
    assert selection.backlog_selected == 10
    assert selection.backlog_remaining == 5
    assert [entry.update_id for entry in selection.selected[:1]] == [100]
    assert len(selection.selected) == 11


def test_ambiguous_entries_are_never_selected_automatically(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 5, InboxStatus.AMBIGUOUS)
    _seed_entry(tmp_path, 6, InboxStatus.ENQUEUED)

    maintenance = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW,
    )
    real_event = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=frozenset({6}),
        now=NOW,
    )

    assert maintenance.selected == ()
    assert [entry.update_id for entry in real_event.selected] == [6]


def test_entry_decided_is_resumable_without_being_an_entry_event(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 7, InboxStatus.ENTRY_DECIDED)

    selection = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW,
    )

    assert [entry.update_id for entry in selection.selected] == [7]
    assert selection.maintenance_only is True


def test_maintenance_never_selects_due_undecided_entries(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 8, InboxStatus.ENQUEUED)

    selection = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW + timedelta(seconds=INBOX_RETRY_DELAY_SECONDS + 1),
    )

    assert selection.selected == ()
    assert selection.retry_selected == 0
    assert selection.backlog_seen == 1


def test_new_event_and_due_retry_are_deduplicated_by_update_id(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 9, InboxStatus.ENQUEUED)

    selection = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=frozenset({9}),
        now=NOW + timedelta(seconds=INBOX_RETRY_DELAY_SECONDS + 1),
    )

    assert [entry.update_id for entry in selection.selected] == [9]
    assert selection.new_event_updates == (9,)
    assert selection.retry_selected == 0


def test_terminal_entries_are_selected_for_bounded_cleanup(tmp_path: Path) -> None:
    _seed_entry(tmp_path, 40, InboxStatus.ENQUEUED)
    _seed_entry(tmp_path, 41, InboxStatus.DELIVERED)
    _seed_entry(tmp_path, 42, InboxStatus.TERMINAL)
    _seed_entry(tmp_path, 43, InboxStatus.AMBIGUOUS)

    maintenance = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW,
    )
    real_event = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=frozenset({40}),
        now=NOW,
    )

    assert [entry.update_id for entry in maintenance.selected] == [41, 42]
    assert maintenance.maintenance_only is True
    assert maintenance.backlog_seen == 1
    assert maintenance.backlog_remaining == 1
    assert [entry.update_id for entry in real_event.selected] == [40, 41, 42]
    assert real_event.new_event_updates == (40,)


def test_needs_attention_counts_actual_status_not_enqueued_attempts(
    tmp_path: Path,
) -> None:
    _seed_entry(tmp_path, 50, InboxStatus.NEEDS_ATTENTION)
    legacy = enqueue_envelope(
        _envelope(51, 1051, "legacy exhausted attempt"),
        now=NOW,
        root=tmp_path,
        bot_key=BOT,
    )
    save_state(
        legacy.model_copy(update={"attempts": INBOX_MAX_ATTEMPTS}),
        root=tmp_path,
        bot_key=BOT,
    )

    maintenance = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=None,
        now=NOW,
    )
    real_event = select_drain_entries(
        entries(root=tmp_path, bot_key=BOT),
        new_update_ids=frozenset({51}),
        now=NOW,
    )

    assert maintenance.needs_attention == 1
    assert maintenance.selected == ()
    assert real_event.needs_attention == 1
    assert [entry.update_id for entry in real_event.selected] == [51]


def test_publish_writes_complete_json_to_unique_temp_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, dict[str, object]]] = []
    original_replace = inbox_module.os.replace

    def checking_replace(source: str, destination: str) -> None:
        source_path = Path(source)
        assert source_path.name.startswith(".")
        assert source_path.name.endswith(".tmp")
        parsed = json.loads(source_path.read_text(encoding="utf-8"))
        observed.append((source_path.name, parsed))
        original_replace(source, destination)

    monkeypatch.setattr(inbox_module.os, "replace", checking_replace)
    entry = enqueue_envelope(
        _envelope(52, 1052, "atomic"), now=NOW, root=tmp_path, bot_key=BOT
    )

    assert entry.update_id == 52
    assert len(observed) == 1
    temp_name, parsed = observed[0]
    assert temp_name != "52.json"
    assert parsed["update_id"] == 52
    assert sorted(path.name for path in incoming_path(tmp_path, BOT, 52).parent.iterdir()) == [
        "52.json"
    ]


def test_partial_final_record_is_replaced_with_a_complete_duplicate(
    tmp_path: Path,
) -> None:
    incoming_path(tmp_path, BOT, 53).parent.mkdir(parents=True)
    incoming_path(tmp_path, BOT, 53).write_text(
        '{"schema_version": "tawg.telegram-inbox-envelope.v1", "update_id":',
        encoding="utf-8",
    )

    entry = enqueue_envelope(
        _envelope(53, 1053, "recovered"), now=NOW, root=tmp_path, bot_key=BOT
    )

    assert entry.update_id == 53
    stored = json.loads(incoming_path(tmp_path, BOT, 53).read_text(encoding="utf-8"))
    assert stored["update_id"] == 53
    assert stored["envelope"]["text"] == "recovered"


def test_duplicate_enqueue_keeps_the_existing_complete_record(
    tmp_path: Path,
) -> None:
    envelope = _envelope(54, 1054, "first")
    enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    before = incoming_path(tmp_path, BOT, 54).read_bytes()

    duplicate = enqueue_envelope(
        envelope,
        now=NOW + timedelta(hours=1),
        root=tmp_path,
        bot_key=BOT,
    )

    assert duplicate.update_id == 54
    assert incoming_path(tmp_path, BOT, 54).read_bytes() == before


def test_stale_duplicate_write_after_tombstone_is_orphaned_not_resurrected(
    tmp_path: Path,
) -> None:
    envelope = _envelope(55, 1055, "terminal")
    entry = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    payload = incoming_path(tmp_path, BOT, 55).read_bytes()
    remove_entry(entry, root=tmp_path, bot_key=BOT)
    assert done_path(tmp_path, BOT, 55).exists()

    # A duplicate endpoint that only sees a stale snapshot can still publish after
    # the tombstone. The tombstone must win for every reader, and cleanup removes it.
    _publish(incoming_path(tmp_path, BOT, 55), payload)

    assert entries(root=tmp_path, bot_key=BOT) == ()
    assert cleanup_terminal_orphans(root=tmp_path, bot_key=BOT) == 1
    assert not incoming_path(tmp_path, BOT, 55).exists()
    assert done_path(tmp_path, BOT, 55).exists()


def test_save_state_publishes_atomically_without_temp_files(tmp_path: Path) -> None:
    envelope = _envelope(56, 1056, "state")
    entry = enqueue_envelope(envelope, now=NOW, root=tmp_path, bot_key=BOT)
    save_state(
        entry.model_copy(update={"status": InboxStatus.DELIVERY_PENDING}),
        root=tmp_path,
        bot_key=BOT,
    )

    state_directory = state_path(tmp_path, BOT, 56).parent
    assert sorted(path.name for path in state_directory.iterdir()) == ["56.json"]
    stored = load_state(tmp_path, BOT, 56)
    assert stored is not None
    assert stored.status is InboxStatus.DELIVERY_PENDING
