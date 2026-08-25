import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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


def fixture_updates() -> list[dict[str, Any]]:
    return json.loads((ROOT / "tests/fixtures/telegram_updates.json").read_text())


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
    assert records[1]["relations"][0]["target_record_id"] == "tg:tawg:501"
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
