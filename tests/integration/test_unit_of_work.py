import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.unit_of_work import RepositoryUnitOfWork


def _record(record_id: str) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/2026/08/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        updated_at=datetime(2026, 8, 23, tzinfo=UTC),
        text_original="hello",
        ingested_at=datetime(2026, 8, 23, tzinfo=UTC),
    )


def test_batch_and_cursor_roll_back_when_second_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = tmp_path / "data/state/source-cursors.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text('{"telegram_offset":0}\n', encoding="utf-8")
    uow = RepositoryUnitOfWork(tmp_path, operation_id="telegram-43")
    uow.stage_records("data/telegram/2026/08/messages.jsonl", [_record("tg:tawg:42")])
    uow.stage_json("data/state/source-cursors.json", {"telegram_offset": 43})
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: str | bytes | Path, target: str | bytes | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected publish failure"):
        uow.publish()

    assert not (tmp_path / "data/telegram/2026/08/messages.jsonl").exists()
    assert json.loads(cursor.read_text())["telegram_offset"] == 0


def test_successful_publish_is_idempotent(tmp_path: Path) -> None:
    cursor = tmp_path / "data/state/source-cursors.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text('{"telegram_offset":0}\n', encoding="utf-8")

    first = RepositoryUnitOfWork(tmp_path, operation_id="telegram-43")
    first.stage_records("data/telegram/2026/08/messages.jsonl", [_record("tg:tawg:42")])
    first.stage_json("data/state/source-cursors.json", {"telegram_offset": 43})
    assert first.publish().changed_paths == (
        "data/telegram/2026/08/messages.jsonl",
        "data/state/source-cursors.json",
    )

    replay = RepositoryUnitOfWork(tmp_path, operation_id="telegram-43-replay")
    replay.stage_records("data/telegram/2026/08/messages.jsonl", [_record("tg:tawg:42")])
    replay.stage_json("data/state/source-cursors.json", {"telegram_offset": 43})
    assert replay.publish().changed_paths == ()
