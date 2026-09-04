import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    RouteContextScope,
    SourceRecord,
    SourceType,
    TelegramWebhookReceipts,
    TriggerKind,
)
from tawg_bot.persist_mode import PersistMode
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.telegram_intake import MemberWelcomeReconciler, ingest_envelopes
from tawg_bot.telegram_webhook import (
    TelegramWebhookAttachment,
    TelegramWebhookConfig,
    TelegramWebhookEntity,
    TelegramWebhookEnvelope,
    TelegramWebhookNormalizer,
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
    has_bot_command: bool = False,
    entities: tuple[TelegramWebhookEntity, ...] = (),
    attachments: tuple[TelegramWebhookAttachment, ...] = (),
    public_username: str = "alice_tawg",
    display_name: str = "Alice",
    created_at: datetime = NOW,
) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(created_at.timestamp()),
        edited=edited,
        edited_timestamp=edited_timestamp,
        text=text,
        public_username=public_username,
        display_name=display_name,
        author_is_bot=author_is_bot,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        entities=entities,
        has_bot_command=has_bot_command,
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


def test_bot_command_cannot_fall_through_to_greeting_candidate(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    command = envelope(
        update_id=502,
        message_id=42,
        text="/hello",
        entities=(
            TelegramWebhookEntity(
                entity_type="bot_command", offset=0, length=6, value="/hello"
            ),
        ),
        has_bot_command=True,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(command,),
        now=NOW,
    )

    assert result.persisted == 1
    assert result.jobs_created == 0
    assert json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    ) == []


def test_ingest_envelopes_namespaces_receipt_by_bot(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(envelope(update_id=1001, message_id=60, text="hi"),),
        now=NOW,
        bot_id=77,
        persist_mode=PersistMode.RECEIPT_ONLY,
    )
    assert (tmp_path / "data/state/telegram-webhook-receipts.77.json").exists()
    assert not (tmp_path / "data/state/telegram-webhook-receipts.json").exists()


def test_receipt_only_creates_bot_local_job_even_when_record_exists(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    # Production ingests first in full mode with a higher update_id.
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(
            envelope(
                update_id=999999,
                message_id=70,
                text="@tawg_bot hi",
                triggers_reply=True,
                entities=(
                    TelegramWebhookEntity(
                        entity_type="mention", offset=0, length=9, value="@tawg_bot"
                    ),
                ),
            ),
        ),
        now=NOW,
    )
    # Dev ingests the same message with a lower update_id in receipt-only mode.
    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(
            envelope(
                update_id=100,
                message_id=70,
                text="@tawg_bot hi",
                triggers_reply=True,
                entities=(
                    TelegramWebhookEntity(
                        entity_type="mention", offset=0, length=9, value="@tawg_bot"
                    ),
                ),
            ),
        ),
        now=NOW,
        bot_id=77,
        persist_mode=PersistMode.RECEIPT_ONLY,
    )
    assert result.jobs_created == 1
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert [job["job_id"] for job in jobs] == ["reply:77:tg:tawg:70", "reply:tg:tawg:70"]


def test_redacted_bot_command_marker_survives_webhook_to_intake(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token="x" * 32,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(tmp_path / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 503,
            "message": {
                "message_id": 43,
                "date": int(NOW.timestamp()),
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "from": {"first_name": "Alice"},
                "text": "/hello alice@example.com",
                "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
            },
        }
    ).encode()

    decision = normalizer.process("x" * 32, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.text == "/hello [REDACTED_EMAIL]"
    assert decision.envelope.entities == ()
    assert decision.envelope.has_bot_command is True
    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(decision.envelope,),
        now=NOW,
    )
    assert result.persisted == 1
    assert result.jobs_created == 0


def test_replay_is_a_noop_but_an_older_unseen_update_is_ingested(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    mention = envelope(
        update_id=900,
        message_id=90,
        text="@tawg_bot ask",
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
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
        "message_thread_id": 12,
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


def test_delivered_bot_message_ids_reads_bot_scoped_state(tmp_path: Path) -> None:
    """A receipt-only worker persists its delivered ids under
    ``delivery-state.<bot_id>.json``. The intake layer must recognise a reply to that
    worker's own message as REPLY_TO_BOT, not only replies to the shared worker."""
    from tawg_bot.telegram_intake import _delivered_bot_message_ids

    (tmp_path / "data/state").mkdir(parents=True, exist_ok=True)
    seed_delivered_bot_message(tmp_path, 900)
    bot_scoped = tmp_path / "data/state/delivery-state.8750877254.json"
    bot_scoped.write_text(
        (tmp_path / "data/state/delivery-state.json").read_text(),
        encoding="utf-8",
    )
    payload = json.loads(bot_scoped.read_text())
    payload[0]["telegram_message_ids"] = [901]
    bot_scoped.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    ids = _delivered_bot_message_ids(tmp_path, chat_id=-100424242)
    assert ids == {900, 901}


def test_member_welcome_creates_one_immediate_job_and_one_l1_introduction_job(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=720,
        message_id=80,
        text="Happy to be here",
        public_username="LoganVerdict",
        display_name="Logan",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=721,
        message_id=81,
        text="Welcome, Logan 👋",
        reply_to_message_id=80,
        message_thread_id=77,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == 2
    assert [(job["job_id"], job["trigger_kind"]) for job in jobs] == [
        ("member-introduction:logan", "member_introduction"),
        ("member-welcome:logan", "member_welcome"),
    ]
    direct = next(job for job in jobs if job["trigger_kind"] == "member_welcome")
    introduction = next(
        job for job in jobs if job["trigger_kind"] == "member_introduction"
    )
    assert direct["reply_to_message_id"] is None
    assert direct["message_thread_id"] == 77
    assert direct["welcome_target_person_id"] == "logan"
    assert direct["welcome_target_record_id"] == "tg:tawg:80"
    assert introduction["reply_to_message_id"] is None
    assert introduction["message_thread_id"] == 77
    assert introduction["prerequisite_job_id"] == "member-welcome:logan"


def test_existing_cross_source_member_page_does_not_suppress_first_tawg_welcome(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    profile = tmp_path / "knowledge/acknowledgements/logan.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "---\n"
        "title: Logan\n"
        "type: person\n"
        "created: 2026-08-01\n"
        "updated: 2026-08-01\n"
        "source_ids:\n"
        "  - magicians:member:logan\n"
        "provenance_status: verified\n"
        "---\n\n"
        "## Contributions\n\nReviews Web3 projects on Ethereum Magicians.\n",
        encoding="utf-8",
    )
    joined_member = envelope(
        update_id=730,
        message_id=90,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=731,
        message_id=91,
        text="Welcome, Logan!",
        reply_to_message_id=90,
        message_thread_id=77,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == 2
    assert {job["job_id"] for job in jobs} == {
        "member-welcome:logan",
        "member-introduction:logan",
    }


def test_l1_reconciler_backfills_earliest_historical_welcome_once(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=740,
        message_id=100,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=88,
    )
    first_welcome = envelope(
        update_id=741,
        message_id=101,
        text="Hi Logan, glad to have you here!",
        message_thread_id=88,
        has_bot_command=True,
    )
    later_welcome = envelope(
        update_id=742,
        message_id=102,
        text="Welcome, Logan 👋",
        reply_to_message_id=100,
        message_thread_id=99,
        has_bot_command=True,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, first_welcome, later_welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    ignored_jobs = []
    for record_id, thread_id in (("tg:tawg:101", 88), ("tg:tawg:102", 99)):
        ignored_jobs.append(
            PendingBotJob(
                job_id=f"reply:{record_id}",
                trigger_record_id=record_id,
                reply_to_message_id=int(record_id.rsplit(":", 1)[-1]),
                message_thread_id=thread_id,
                trigger_kind=TriggerKind.GREETING_CANDIDATE,
                status=JobStatus.IGNORED,
                classified_route=BotRoute.IGNORE,
                router_context_scope=RouteContextScope.CONVERSATION,
                router_context_sha256="a" * 64,
                router_version="contextual-ai-v5",
                routed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.model_dump(mode="json") for job in ignored_jobs]) + "\n",
        encoding="utf-8",
    )

    reconciler = MemberWelcomeReconciler(tmp_path)
    assert reconciler.reconcile(now=NOW) == 2
    assert reconciler.reconcile(now=NOW) == 0

    jobs = {
        item["job_id"]: item
        for item in json.loads(
            (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
        )
    }
    assert jobs["member-welcome:logan-verdict"]["trigger_record_id"] == "tg:tawg:101"
    assert jobs["member-welcome:logan-verdict"]["message_thread_id"] == 88
    assert jobs["member-introduction:logan-verdict"]["message_thread_id"] == 88


def test_l1_reconciler_atomically_replaces_pending_generic_welcome(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=743,
        message_id=103,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=88,
    )
    welcome = envelope(
        update_id=744,
        message_id=104,
        text="Welcome, Logan!",
        reply_to_message_id=103,
        message_thread_id=88,
        has_bot_command=True,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    generic = PendingBotJob(
        job_id="reply:tg:tawg:104",
        trigger_record_id="tg:tawg:104",
        reply_to_message_id=104,
        message_thread_id=88,
        trigger_kind=TriggerKind.GREETING_CANDIDATE,
        created_at=NOW,
        updated_at=NOW,
    )
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs_path.write_text(
        json.dumps([generic.model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )

    assert MemberWelcomeReconciler(tmp_path).reconcile(now=NOW) == 2

    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert {job["job_id"] for job in jobs} == {
        "member-welcome:logan-verdict",
        "member-introduction:logan-verdict",
    }


def test_repeated_welcome_keeps_the_first_topic_and_creates_no_generic_reply(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=750,
        message_id=110,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan",
        message_thread_id=88,
    )
    first = envelope(
        update_id=751,
        message_id=111,
        text="Welcome, Logan!",
        reply_to_message_id=110,
        message_thread_id=88,
    )
    later = envelope(
        update_id=752,
        message_id=112,
        text="Welcome Logan 👋",
        reply_to_message_id=110,
        message_thread_id=99,
    )

    first_result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, first),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    later_result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(later,),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert first_result.jobs_created == 2
    assert later_result.jobs_created == 0
    assert len(jobs) == 2
    assert {job["message_thread_id"] for job in jobs} == {88}


def test_standalone_welcome_never_resolves_a_member_from_another_topic(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=755,
        message_id=115,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=99,
    )
    wrong_topic = envelope(
        update_id=756,
        message_id=116,
        text="Welcome Logan!",
        public_username="merlini",
        display_name="Merlini",
        message_thread_id=88,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, wrong_topic),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.jobs_created == 0
    assert json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    ) == []


def test_reply_welcome_never_resolves_a_member_from_another_topic(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=753,
        message_id=113,
        text="Happy to join",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=99,
    )
    wrong_topic = envelope(
        update_id=754,
        message_id=114,
        text="Welcome Logan!",
        reply_to_message_id=113,
        public_username="merlini",
        display_name="Merlini",
        message_thread_id=88,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, wrong_topic),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.jobs_created == 0
    assert json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    ) == []


def test_same_display_name_with_a_distinct_handle_is_still_a_new_tawg_member(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    old_alice = envelope(
        update_id=757,
        message_id=117,
        text="An old message",
        public_username="AliceOld",
        display_name="Alice",
        message_thread_id=77,
        created_at=NOW.replace(day=20),
    )
    new_alice = envelope(
        update_id=758,
        message_id=118,
        text="Happy to join",
        public_username="AliceNew",
        display_name="Alice",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=759,
        message_id=119,
        text="Welcome Alice!",
        reply_to_message_id=118,
        public_username="merlini",
        display_name="Merlini",
        message_thread_id=77,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(old_alice, new_alice, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.jobs_created == 2
    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert {job["welcome_target_record_id"] for job in jobs} == {"tg:tawg:118"}


def test_ambiguous_welcome_fails_closed_without_a_generic_greeting_job(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    alice = envelope(
        update_id=760,
        message_id=120,
        text="Joining the group",
        public_username="alice_new",
        display_name="Alice",
    )
    bob = envelope(
        update_id=761,
        message_id=121,
        text="Joining the group",
        public_username="bob_new",
        display_name="Bob",
    )
    ambiguous = envelope(
        update_id=762,
        message_id=122,
        text="Welcome Alice and Bob!",
        message_thread_id=77,
        public_username="charlie_tawg",
        display_name="Charlie",
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(alice, bob, ambiguous),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == 0
    assert jobs == []


@pytest.mark.parametrize(
    "welcome_text",
    [
        "Glad to have you, Logan!",
        "欢迎 Logan 👋",
    ],
)
def test_member_welcome_phrase_does_not_require_an_unrelated_greeting_word(
    tmp_path: Path,
    welcome_text: str,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=770,
        message_id=130,
        text="Joining the group",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=771,
        message_id=131,
        text=welcome_text,
        message_thread_id=77,
    )

    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    assert result.jobs_created == 2
    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert {job["trigger_kind"] for job in jobs} == {
        "member_welcome",
        "member_introduction",
    }


def test_editing_away_a_welcome_cancels_undelivered_member_jobs(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=780,
        message_id=140,
        text="Joining the group",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=781,
        message_id=141,
        text="Welcome, Logan!",
        message_thread_id=77,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    edited = envelope(
        update_id=782,
        message_id=141,
        text="Correction: that was meant for another chat.",
        message_thread_id=77,
        edited=True,
        edited_timestamp=int(NOW.timestamp()) + 60,
    )

    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited,),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert jobs == []


def test_editing_the_member_identity_invalidates_old_member_jobs(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=786,
        message_id=144,
        text="Joining the group",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=787,
        message_id=145,
        text="Welcome, Logan!",
        message_thread_id=77,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    original_jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    old_person_id = original_jobs[0]["welcome_target_person_id"]
    edited_member = envelope(
        update_id=788,
        message_id=144,
        text="Joining the group",
        public_username="DifferentHandle",
        display_name="Different Person",
        message_thread_id=77,
        edited=True,
        edited_timestamp=int(NOW.timestamp()) + 60,
    )

    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited_member,),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    refreshed = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert all(
        job.get("welcome_target_person_id") != old_person_id for job in refreshed
    )


def test_editing_a_welcome_resets_ready_copy_before_reconciliation(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    joined_member = envelope(
        update_id=783,
        message_id=142,
        text="Joining the group",
        public_username="LoganVerdict",
        display_name="Logan Verdict",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=784,
        message_id=143,
        text="Welcome, Logan!",
        message_thread_id=77,
    )
    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined_member, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
    )
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    direct = next(item for item in jobs if item["trigger_kind"] == "member_welcome")
    direct.update(
        {
            "status": "ready",
            "prepared_reply_text": "@LoganVerdict stale welcome",
            "prepared_language": "en",
            "classified_route": "coordination",
            "router_context_scope": "conversation",
            "router_context_sha256": "a" * 64,
            "router_version": "deterministic-member-welcome-v1",
            "routed_at": NOW.isoformat(),
        }
    )
    jobs_path.write_text(json.dumps(jobs) + "\n", encoding="utf-8")
    edited = envelope(
        update_id=785,
        message_id=143,
        text="Welcome Logan — glad to have you here!",
        message_thread_id=77,
        edited=True,
        edited_timestamp=int(NOW.timestamp()) + 60,
    )

    ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(edited,),
        now=NOW,
        telegram_chat_id=-100424242,
    )

    refreshed = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert len(refreshed) == 2
    refreshed_direct = next(
        item for item in refreshed if item["trigger_kind"] == "member_welcome"
    )
    assert refreshed_direct["status"] == "pending"
    assert refreshed_direct["prepared_reply_text"] is None


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


def test_bot_command_entity_without_command_marker_is_rejected(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    inconsistent = envelope(
        update_id=802,
        message_id=82,
        text="/hello",
        entities=(
            TelegramWebhookEntity(
                entity_type="bot_command", offset=0, length=6, value="/hello"
            ),
        ),
    )

    with pytest.raises(ValueError, match="command metadata"):
        ingest_envelopes(
            root=tmp_path,
            group_slug="tawg",
            bot_username="tawg_bot",
            envelopes=(inconsistent,),
            now=NOW,
        )

    assert not (tmp_path / "data/telegram").exists()
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
