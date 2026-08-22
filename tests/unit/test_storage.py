from datetime import UTC, datetime
from pathlib import Path

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection


def _record(record_id: str, text: str) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        updated_at=datetime(2026, 8, 23, tzinfo=UTC),
        text_original=text,
        ingested_at=datetime(2026, 8, 23, tzinfo=UTC),
    )


def test_jsonl_upsert_replaces_stable_id_and_sorts_records(tmp_path: Path) -> None:
    path = tmp_path / "messages.jsonl"
    collection = JsonlCollection(path, SourceRecord)
    path.write_bytes(collection.merged_bytes([_record("tg:tawg:2", "old")]))

    merged = collection.merged_bytes(
        [_record("tg:tawg:2", "edited"), _record("tg:tawg:1", "first")]
    )
    decoded = collection.decode(merged)

    assert [record.record_id for record in decoded] == ["tg:tawg:1", "tg:tawg:2"]
    assert decoded[1].text_original == "edited"


def test_jsonl_replay_keeps_identical_bytes(tmp_path: Path) -> None:
    path = tmp_path / "messages.jsonl"
    collection = JsonlCollection(path, SourceRecord)
    record = _record("tg:tawg:1", "same")
    first = collection.merged_bytes([record])
    path.write_bytes(first)

    assert collection.merged_bytes([record]) == first
