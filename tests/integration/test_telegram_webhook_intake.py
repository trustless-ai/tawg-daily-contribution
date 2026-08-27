import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tawg_bot.models import SourceRecord, SourceType, TelegramWebhookReceipts
from tawg_bot.telegram_intake import ingest_envelopes
from tawg_bot.telegram_webhook import (
    TelegramWebhookAttachment,
    TelegramWebhookEntity,
    TelegramWebhookEnvelope,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


class FailingUnitOfWork(RepositoryUnitOfWork):
    def publish(self):
        raise RuntimeError("injected before publish")


def seed_repository(root: Path) -> None:
    (root / "config").mkdir()
    (root / "knowledge/meta").mkdir(parents=True)
    (root / "data/state").mkdir(parents=True)
    (root / "config/privacy.yml").write_bytes((ROOT / "config/privacy.yml").read_bytes())
    (root / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    (root / "data/state/pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")


def envelope(
    *,
    update_id: int,
    message_id: int,
    text: str,
    triggers_reply: bool = False,
    edited: bool = False,
    edited_timestamp: int | None = None,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    author_is_bot: bool = False,
    entities: tuple[TelegramWebhookEntity, ...] = (),
    attachments: tuple[TelegramWebhookAttachment, ...] = (),
) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=edited,
        edited_timestamp=edited_timestamp,
        text=text,
        public_username="alice_tawg",
        display_name="Alice",
        author_is_bot=author_is_bot,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        entities=entities,
        attachments=attachments,
        triggers_reply=triggers_reply,
        integrity_digest="0" * 64,
    )
    payload = value.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value.model_copy(update={"integrity_digest": digest})


def read_records(root: Path) -> list[dict[str, object]]:
    path = root / "data/telegram/2026/08/messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def seed_delivered_bot_message(
    root: Path, message_id: int, *, chat_id: int = -100424242
) -> None:
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    delivery = {
        "schema_version": "tawg.delivery-attempt.v1",
        "delivery_id": "reply:tg:tawg:899",
        "job_id": "reply:tg:tawg:899",
        "destination": "tawg",
        "status": "delivered",
        "content_sha256": "a" * 64,
        "message_count": 1,
        "delivery_format": "rich_markdown_v1",
        "reply_to_message_id": 899,
        "message_thread_id": 16,
        "telegram_chat_id": chat_id,
        "telegram_message_ids": [message_id],
        "sent_at": timestamp,
        "prepared_at": timestamp,
        "updated_at": timestamp,
        "safe_error_code": None,
    }
    (root / "data/state/delivery-state.json").write_text(
        json.dumps([delivery]) + "\n", encoding="utf-8"
    )


def test_envelopes_persist_context_and_create_jobs_only_for_reply_triggers(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    context = envelope(update_id=500, message_id=40, text="Context for the group")
    mention = envelope(
        update_id=501,
        message_id=41,
        text="@tawg_bot can you summarize?",
        triggers_reply=True,
        message_thread_id=7,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(context, mention),
        now=NOW,
    )

    assert result.received == 2
    assert result.persisted == 2
    assert result.jobs_created == 1
    assert [record["record_id"] for record in read_records(tmp_path)] == [
        "tg:tawg:40",
        "tg:tawg:41",
    ]
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert [job["job_id"] for job in jobs] == ["reply:tg:tawg:41"]
    assert jobs[0]["message_thread_id"] == 7
    assert jobs[0]["trigger_kind"] == "mention"
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts == {
        "schema_version": "tawg.telegram-webhook-receipts.v1",
        "update_ids": [500, 501],
    }
    assert not (tmp_path / "data/state/source-cursors.json").exists()


def test_replay_is_a_noop_but_an_older_unseen_update_is_ingested(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    mention = envelope(
        update_id=900,
        message_id=90,
        text="/ask",
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="bot_command", offset=0, length=4, value="/ask"
            ),
        ),
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(mention,),
        now=NOW,
    )

    replay = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(mention,),
        now=NOW,
    )
    older = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(envelope(update_id=100, message_id=10, text="Older context"),),
        now=NOW,
    )

    assert replay.persisted == 0
    assert replay.jobs_created == 0
    assert replay.changed_paths == ()
    assert older.persisted == 1
    assert [record["record_id"] for record in read_records(tmp_path)] == [
        "tg:tawg:10",
        "tg:tawg:90",
    ]
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert [job["job_id"] for job in jobs] == ["reply:tg:tawg:90"]
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [100, 900]


def test_edited_envelope_updates_safe_content_without_changing_record_or_job_identity(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    original = envelope(
        update_id=700,
        message_id=70,
        text="@tawg_bot first text",
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
        attachments=(TelegramWebhookAttachment(media_type="photo", has_caption=True),),
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(original,),
        now=NOW,
    )
    edit_time = int(NOW.timestamp()) + 60
    edited = envelope(
        update_id=701,
        message_id=70,
        text="@tawg_bot corrected text",
        triggers_reply=True,
        edited=True,
        edited_timestamp=edit_time,
        reply_to_message_id=65,
        message_thread_id=12,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
        attachments=(TelegramWebhookAttachment(media_type="file", has_caption=True),),
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited,),
        now=NOW,
    )

    assert result.persisted == 1
    assert result.jobs_created == 0
    records = read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["record_id"] == "tg:tawg:70"
    assert records[0]["text_original"] == "@tawg_bot corrected text"
    assert records[0]["updated_at"] == "2026-08-23T00:01:00Z"
    assert records[0]["relations"] == [
        {"relation_type": "reply_to", "target_record_id": "tg:tawg:65"}
    ]
    assert records[0]["attachment_metadata"] == [
        {"has_caption": True, "interpreted": False, "media_type": "file"}
    ]
    assert records[0]["source_payload"] == {
        "author_is_bot": False,
        "edited": True,
        "message_kind": "group_message",
        "update_id": 701,
    }
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "reply:tg:tawg:70"
    assert jobs[0]["message_thread_id"] == 12


def test_webhook_direct_reply_and_greeting_use_the_shared_trigger_classifier(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 900)
    direct_reply = envelope(
        update_id=710,
        message_id=71,
        text="one more detail",
        reply_to_message_id=900,
        message_thread_id=16,
    )
    greeting = envelope(update_id=711, message_id=72, text="good morning everyone")
    bot_echo = envelope(
        update_id=712,
        message_id=73,
        text="@tawg_bot loop",
        author_is_bot=True,
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(direct_reply, greeting, bot_echo),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.persisted == 3
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert [(job["job_id"], job["trigger_kind"]) for job in jobs] == [
        ("reply:tg:tawg:71", "reply_to_bot"),
        ("reply:tg:tawg:72", "greeting_candidate"),
    ]
    assert jobs[0]["message_thread_id"] == 16


def test_webhook_direct_reply_audit_is_bound_to_the_configured_chat(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 900, chat_id=-100999999)

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(
            envelope(
                update_id=713,
                message_id=74,
                text="unrelated cross-chat reply",
                reply_to_message_id=900,
            ),
        ),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.persisted == 1
    assert result.jobs_created == 0
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert jobs == []


@pytest.mark.parametrize("edit_delay_seconds", [0, 60])
def test_edit_first_original_second_keeps_the_edited_same_record_content(
    tmp_path: Path,
    edit_delay_seconds: int,
) -> None:
    seed_repository(tmp_path)
    edit_time = int(NOW.timestamp()) + edit_delay_seconds
    edited = envelope(
        update_id=701,
        message_id=70,
        text="corrected text",
        edited=True,
        edited_timestamp=edit_time,
    )
    delayed_original = envelope(
        update_id=700,
        message_id=70,
        text="stale original text",
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited, delayed_original),
        now=NOW,
    )

    assert result.persisted == 1
    assert read_records(tmp_path)[0]["text_original"] == "corrected text"
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [700, 701]


def test_delayed_original_receipt_does_not_overwrite_a_persisted_edit(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    edited = envelope(
        update_id=701,
        message_id=70,
        text="persisted corrected text",
        edited=True,
        edited_timestamp=int(NOW.timestamp()) + 60,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited,),
        now=NOW,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(
            envelope(
                update_id=700,
                message_id=70,
                text="stale original text",
            ),
        ),
        now=NOW,
    )

    assert result.persisted == 0
    assert read_records(tmp_path)[0]["text_original"] == "persisted corrected text"
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [700, 701]


@pytest.mark.parametrize("newer_first", [False, True])
def test_same_second_edits_use_update_id_across_batch_order(
    tmp_path: Path,
    newer_first: bool,
) -> None:
    seed_repository(tmp_path)
    edit_time = int(NOW.timestamp()) + 60
    older = envelope(
        update_id=701,
        message_id=70,
        text="same-second older edit",
        edited=True,
        edited_timestamp=edit_time,
    )
    newer = envelope(
        update_id=702,
        message_id=70,
        text="same-second newer edit",
        edited=True,
        edited_timestamp=edit_time,
    )

    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(newer, older) if newer_first else (older, newer),
        now=NOW,
    )

    record = read_records(tmp_path)[0]
    assert record["text_original"] == "same-second newer edit"
    assert record["source_payload"]["update_id"] == 702
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [701, 702]


@pytest.mark.parametrize(
    ("first_update_id", "second_update_id", "expected_text", "expected_persisted"),
    [
        (701, 702, "second edit", 1),
        (702, 701, "first edit", 0),
    ],
)
def test_same_second_edits_use_persisted_update_id_across_requests(
    tmp_path: Path,
    first_update_id: int,
    second_update_id: int,
    expected_text: str,
    expected_persisted: int,
) -> None:
    seed_repository(tmp_path)
    edit_time = int(NOW.timestamp()) + 60
    first = envelope(
        update_id=first_update_id,
        message_id=70,
        text="first edit",
        edited=True,
        edited_timestamp=edit_time,
    )
    second = envelope(
        update_id=second_update_id,
        message_id=70,
        text="second edit",
        edited=True,
        edited_timestamp=edit_time,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(first,),
        now=NOW,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(second,),
        now=NOW,
    )

    record = read_records(tmp_path)[0]
    assert result.persisted == expected_persisted
    assert record["text_original"] == expected_text
    assert record["source_payload"]["update_id"] == max(
        first_update_id,
        second_update_id,
    )
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [701, 702]


def test_unknown_legacy_update_id_fails_closed_on_same_second_edit(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    edit_time = NOW.replace(minute=1)
    legacy = SourceRecord.from_text(
        record_id="tg:tawg:70",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:70",
        author_person_id=None,
        author_source_handle="Alice",
        created_at=NOW,
        updated_at=edit_time,
        text_original="legacy newer content",
        ingested_at=NOW,
        source_payload={"message_kind": "group_message", "edited": True},
    )
    history = tmp_path / "data/telegram/2026/08/messages.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(legacy.model_dump_json() + "\n", encoding="utf-8")
    delayed = envelope(
        update_id=700,
        message_id=70,
        text="@tawg_bot delayed same-second edit",
        triggers_reply=True,
        edited=True,
        edited_timestamp=int(edit_time.timestamp()),
        entities=(
            TelegramWebhookEntity(
                entity_type="mention",
                offset=0,
                length=9,
                value="@tawg_bot",
            ),
        ),
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(delayed,),
        now=NOW,
    )

    assert result.persisted == 0
    assert result.jobs_created == 0
    assert read_records(tmp_path)[0]["text_original"] == "legacy newer content"
    receipts = json.loads(
        (tmp_path / "data/state/telegram-webhook-receipts.json").read_text()
    )
    assert receipts["update_ids"] == [700]
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert jobs == []


def test_tampered_envelope_is_rejected_before_any_durable_ingestion(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    valid = envelope(update_id=800, message_id=80, text="safe text")
    tampered = valid.model_copy(update={"text": "changed after normalization"})

    with pytest.raises(ValueError, match="integrity"):
        ingest_envelopes(
            root=tmp_path,
            group_slug="tawg",
            bot_username="tawg_bot",
            envelopes=(tampered,),
            now=NOW,
        )

    assert not (tmp_path / "data/telegram").exists()
    assert not (tmp_path / "data/state/telegram-webhook-receipts.json").exists()
    assert json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text()) == []


def test_reply_trigger_without_a_valid_entity_is_rejected_before_ingestion(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    inconsistent = envelope(
        update_id=801,
        message_id=81,
        text="ordinary context",
        triggers_reply=True,
    )

    with pytest.raises(ValueError, match="trigger"):
        ingest_envelopes(
            root=tmp_path,
            group_slug="tawg",
            bot_username="tawg_bot",
            envelopes=(inconsistent,),
            now=NOW,
        )

    assert not (tmp_path / "data/telegram").exists()
    assert not (tmp_path / "data/state/telegram-webhook-receipts.json").exists()
    assert json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text()) == []


def test_webhook_receipts_keep_only_the_highest_2048_distinct_update_ids() -> None:
    receipts = TelegramWebhookReceipts(
        schema_version="tawg.telegram-webhook-receipts.v1",
        update_ids=[0, 1, 1, *range(2, 2050)],
    )

    assert len(receipts.update_ids) == 2048
    assert receipts.update_ids[0] == 2
    assert receipts.update_ids[-1] == 2049


@pytest.mark.parametrize(
    "payload",
    [
        {"update_ids": [500]},
        {"schema_version": "tawg.telegram-webhook-receipts.v0", "update_ids": [500]},
    ],
)
def test_webhook_receipts_reject_missing_or_wrong_schema(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TelegramWebhookReceipts.model_validate(payload)


def test_failed_publish_keeps_receipts_records_jobs_and_aliases_unchanged(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    aliases_before = (tmp_path / "knowledge/meta/aliases.yml").read_bytes()
    value = envelope(
        update_id=950,
        message_id=95,
        text="@tawg_bot persist atomically",
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="injected"):
        ingest_envelopes(
            root=tmp_path,
            group_slug="tawg",
            bot_username="tawg_bot",
            envelopes=(value,),
            now=NOW,
            uow_factory=lambda root, operation_id: FailingUnitOfWork(
                root, operation_id=operation_id
            ),
        )

    assert not (tmp_path / "data/telegram").exists()
    assert not (tmp_path / "data/state/telegram-webhook-receipts.json").exists()
    assert json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text()) == []
    assert (tmp_path / "knowledge/meta/aliases.yml").read_bytes() == aliases_before
