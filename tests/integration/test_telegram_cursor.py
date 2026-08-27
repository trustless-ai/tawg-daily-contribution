import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.models import Relation, SourceRecord, SourceType
from tawg_bot.query import SourceQuery
from tawg_bot.telegram_intake import TelegramIntake
from tawg_bot.unit_of_work import RepositoryUnitOfWork

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


class FakeTelegramApi:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.offsets: list[int] = []

    async def get_all_updates(self, offset: int, limit: int = 100) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return self.updates


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
    (root / "data/state/source-cursors.json").write_text(
        json.dumps(
            {
                "schema_version": "tawg.source-cursors.v1",
                "telegram_offset": 100,
                "github": {},
                "magicians": {},
                "knowledge_record_id": None,
            }
        ),
        encoding="utf-8",
    )
    (root / "data/state/pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")
    (root / "data/state/delivery-state.json").write_text("[]\n", encoding="utf-8")


def fixture_updates() -> list[dict[str, Any]]:
    return json.loads((ROOT / "tests/fixtures/telegram_updates.json").read_text())


def telegram_update(
    message_id: int,
    text: str,
    *,
    update_id: int | None = None,
    is_bot: bool = False,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = 16,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": int(NOW.timestamp()) + message_id,
        "chat": {"id": -100424242, "type": "supergroup", "title": "TAWG"},
        "from": {
            "id": 10_000 + message_id,
            "is_bot": is_bot,
            "first_name": "Member",
            "username": f"member_{message_id}",
        },
        "text": text,
        "entities": [],
    }
    if message_thread_id is not None:
        message["message_thread_id"] = message_thread_id
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id or message_id, "message": message}


def seed_delivered_bot_message(
    root: Path,
    message_id: int,
    *,
    thread_id: int = 16,
    chat_id: int = -100424242,
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
        "message_thread_id": thread_id,
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


@pytest.mark.asyncio
async def test_direct_reply_to_a_delivered_bot_message_creates_a_reply_job(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 900)

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(
            [
                telegram_update(
                    901,
                    "RVR stands for Recomputable Verification Receipts.",
                    reply_to_message_id=900,
                )
            ]
        ),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == 1
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "reply:tg:tawg:901"
    assert jobs[0]["trigger_record_id"] == "tg:tawg:901"
    assert jobs[0]["reply_to_message_id"] == 901
    assert jobs[0]["message_thread_id"] == 16
    assert jobs[0]["trigger_kind"] == "reply_to_bot"


@pytest.mark.asyncio
async def test_direct_reply_audit_is_bound_to_the_configured_chat(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 900, chat_id=-100999999)

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(
            [telegram_update(901, "This is unrelated.", reply_to_message_id=900)]
        ),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    assert result.jobs_created == 0
    assert json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    ) == []


@pytest.mark.asyncio
async def test_direct_reply_takes_precedence_over_a_redundant_explicit_mention(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 900)
    update = telegram_update(
        901,
        "@tawg_helper what did you mean?",
        reply_to_message_id=900,
    )
    update["message"]["entities"] = [
        {"type": "mention", "offset": 0, "length": len("@tawg_helper")}
    ]

    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([update]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert jobs[0]["trigger_kind"] == "reply_to_bot"


@pytest.mark.asyncio
async def test_missing_reply_job_is_reconciled_after_the_source_cursor_advanced(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 3467)
    pavlo = next(
        record
        for record in SourceQuery(ROOT).records()
        if record.record_id == "tg:tawg:3470"
    )
    telegram = tmp_path / "data/telegram/2026/08/messages.jsonl"
    telegram.parent.mkdir(parents=True)
    telegram.write_text(pavlo.model_dump_json() + "\n", encoding="utf-8")

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.received == 0
    assert result.jobs_created == 1
    assert jobs[0]["job_id"] == "reply:tg:tawg:3470"
    assert jobs[0]["trigger_kind"] == "reply_to_bot"
    assert jobs[0]["message_thread_id"] == 16


@pytest.mark.asyncio
async def test_reconciliation_never_backfills_a_reply_from_another_bot(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    seed_delivered_bot_message(tmp_path, 3467)
    bot_reply = SourceRecord.from_text(
        record_id="tg:tawg:3471",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:3471",
        author_person_id="other-bot",
        author_source_handle="Trusty Assistant",
        created_at=NOW,
        updated_at=NOW,
        text_original="Automated acknowledgement",
        ingested_at=NOW,
        relations=[Relation(relation_type="reply_to", target_record_id="tg:tawg:3467")],
        source_payload={"message_kind": "group_message", "message_thread_id": 16},
    )
    telegram = tmp_path / "data/telegram/2026/08/messages.jsonl"
    telegram.parent.mkdir(parents=True)
    telegram.write_text(bot_reply.model_dump_json() + "\n", encoding="utf-8")

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == 0
    assert jobs == []


@pytest.mark.asyncio
async def test_every_greeting_candidate_is_sent_to_the_router_even_inside_prose(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    greetings = (
        "hello",
        "hi",
        "hey",
        "yo",
        "greetings",
        "gm",
        "gn",
        "good morning",
        "morning",
        "good afternoon",
        "afternoon",
        "good evening",
        "evening",
        "good night",
        "how are you",
        "how's it going",
        "how is it going",
        "what's up",
        "whats up",
        "sup",
        "hola",
        "buenos días",
        "buenas tardes",
        "buenas noches",
        "bonjour",
        "bonsoir",
        "salut",
        "hallo",
        "guten morgen",
        "guten tag",
        "guten abend",
        "ciao",
        "buongiorno",
        "buonasera",
        "olá",
        "bom dia",
        "boa tarde",
        "boa noite",
        "namaste",
        "salaam",
        "shalom",
        "konnichiwa",
        "ohayo",
        "привет",
        "здравствуйте",
        "доброе утро",
        "добрый день",
        "добрый вечер",
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "哈啰",
        "嘿",
        "早",
        "早安",
        "早上好",
        "上午好",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "大家好",
        "各位好",
        "你好吗",
        "最近好吗",
        "こんにちは",
        "おはようございます",
        "こんばんは",
        "안녕하세요",
        "좋은 아침",
        "مرحبا",
        "السلام عليكم",
        "صباح الخير",
        "مساء الخير",
        "नमस्ते",
        "सुप्रभात",
    )
    updates = [
        telegram_update(1_000 + index, f"Article example: {greeting}; analysis follows.")
        for index, greeting in enumerate(greetings)
    ]

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(updates),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert result.jobs_created == len(greetings)
    assert {job["trigger_kind"] for job in jobs} == {"greeting_candidate"}


@pytest.mark.asyncio
async def test_greeting_matching_uses_words_and_never_starts_a_bot_loop(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    updates = [
        telegram_update(1_100, "This proposal is ready."),
        telegram_update(1_101, "good morning", is_bot=True),
    ]

    result = await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(updates),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    assert result.jobs_created == 0
    assert json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    ) == []


@pytest.mark.asyncio
async def test_intake_persists_all_target_messages_jobs_and_cursor_atomically(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    updates = fixture_updates()
    updates[2]["message"]["message_thread_id"] = 700
    api = FakeTelegramApi(updates)
    intake = TelegramIntake(
        root=tmp_path,
        api=api,
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    )

    result = await intake.collect(NOW)

    assert result.received == 7
    assert result.persisted == 5
    assert result.filtered == 1
    assert result.jobs_created == 2
    assert result.next_offset == 107
    records_path = tmp_path / "data/telegram/2026/08/messages.jsonl"
    records = [json.loads(line) for line in records_path.read_text().splitlines()]
    assert [record["record_id"] for record in records] == [
        "tg:tawg:501",
        "tg:tawg:503",
        "tg:tawg:504",
        "tg:tawg:505",
        "tg:tawg:506",
    ]
    assert records[0]["text_original"] == "Parser and tests shipped"
    assert records[0]["source_payload"]["update_id"] == 103
    assert records[1]["relations"][0]["target_record_id"] == "tg:tawg:501"
    assert records[1]["source_payload"]["message_thread_id"] == 700
    assert records[2]["source_payload"]["message_thread_id"] is None
    serialized = json.dumps(records)
    assert "11001" not in serialized
    assert "-100424242" not in serialized
    assert "secret-photo-file-id" not in serialized
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert [job["trigger_record_id"] for job in jobs] == ["tg:tawg:503", "tg:tawg:504"]
    assert jobs[0]["message_thread_id"] == 700
    assert jobs[1]["message_thread_id"] is None
    cursors = json.loads((tmp_path / "data/state/source-cursors.json").read_text())
    assert cursors["telegram_offset"] == 107
    assert not (tmp_path / "data/state/telegram-webhook-receipts.json").exists()


@pytest.mark.asyncio
async def test_replayed_batch_after_source_only_checkpoint_is_idempotent(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    api = FakeTelegramApi(fixture_updates())
    intake = TelegramIntake(
        root=tmp_path,
        api=api,
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    )
    await intake.collect(NOW)
    cursor_path = tmp_path / "data/state/source-cursors.json"
    cursor = json.loads(cursor_path.read_text())
    cursor["telegram_offset"] = 100
    cursor_path.write_text(json.dumps(cursor), encoding="utf-8")

    replay_updates = fixture_updates()
    replay_updates[2]["message"]["message_thread_id"] = 700
    replay = TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(replay_updates),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    )
    await replay.collect(NOW)

    records = (tmp_path / "data/telegram/2026/08/messages.jsonl").read_text().splitlines()
    jobs = json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text())
    assert len(records) == 5
    assert len(jobs) == 2
    assert jobs[0]["message_thread_id"] == 700


@pytest.mark.asyncio
async def test_edited_pending_trigger_clears_stale_route_and_prepared_reply(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    first_updates = [fixture_updates()[2]]
    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(first_updates),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs[0].update(
        {
            "status": "ready",
            "prepared_reply_text": "Stale prepared reply",
            "prepared_language": "en",
            "classified_route": "knowledge_question",
            "router_context_sha256": "a" * 64,
            "router_version": "contextual-ai-v1",
            "routed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    edited = fixture_updates()[2]
    edited = {
        "update_id": 107,
        "edited_message": {
            **edited["message"],
            "edit_date": edited["message"]["date"] + 60,
            "text": "@tawg_helper correction: ERC-8004 changed",
        },
    }
    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([edited]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    refreshed = json.loads(jobs_path.read_text(encoding="utf-8"))[0]
    assert refreshed["status"] == "pending"
    assert refreshed["classified_route"] is None
    assert refreshed["router_context_sha256"] is None
    assert refreshed["router_version"] is None
    assert refreshed["routed_at"] is None
    assert refreshed["prepared_reply_text"] is None
    assert refreshed["prepared_language"] is None


@pytest.mark.asyncio
async def test_edit_that_removes_mention_withdraws_an_undelivered_job(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    first = fixture_updates()[2]
    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([first]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs[0].update(
        {
            "status": "ready",
            "prepared_reply_text": "This reply must be withdrawn",
            "prepared_language": "en",
        }
    )
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    edited_message = {
        **first["message"],
        "edit_date": first["message"]["date"] + 60,
        "text": "I withdrew the bot request.",
        "entities": [],
    }
    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(
            [{"update_id": 107, "edited_message": edited_message}]
        ),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    assert json.loads(jobs_path.read_text(encoding="utf-8")) == []


@pytest.mark.asyncio
async def test_edit_that_removes_a_greeting_preserves_the_ignored_audit(
    tmp_path: Path,
) -> None:
    seed_repository(tmp_path)
    first = telegram_update(1_200, "good morning")
    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([first]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs[0].update(
        {
            "status": "ignored",
            "classified_route": "ignore",
            "router_context_sha256": "a" * 64,
            "router_version": "contextual-ai-v2",
            "routed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    edited = {
        "update_id": 1_201,
        "edited_message": {
            **first["message"],
            "edit_date": first["message"]["date"] + 60,
            "text": "The article has been revised.",
        },
    }

    await TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi([edited]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    ).collect(NOW)

    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert len(persisted) == 1
    assert persisted[0]["status"] == "ignored"
    assert persisted[0]["classified_route"] == "ignore"


@pytest.mark.asyncio
async def test_failed_publish_keeps_old_cursor_and_no_partial_records(tmp_path: Path) -> None:
    seed_repository(tmp_path)
    intake = TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(fixture_updates()),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
        uow_factory=lambda root, operation_id: FailingUnitOfWork(root, operation_id=operation_id),
    )

    with pytest.raises(RuntimeError, match="injected"):
        await intake.collect(NOW)

    cursor = json.loads((tmp_path / "data/state/source-cursors.json").read_text())
    assert cursor["telegram_offset"] == 100
    assert not (tmp_path / "data/telegram").exists()
    assert json.loads((tmp_path / "data/state/pending-bot-jobs.json").read_text()) == []


def test_intake_reads_fixed_group_identity_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repository(tmp_path)
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-100424242")
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "@tawg_helper")

    intake = TelegramIntake.from_env(root=tmp_path, api=FakeTelegramApi([]))

    assert intake.chat_id == -100424242
    assert intake.bot_username == "tawg_helper"
