from datetime import UTC, datetime
from pathlib import Path

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import CorruptCollection, JsonlCollection, partition_stable_records


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


def test_partition_stable_records_updates_original_shard_and_sends_new_ids_to_canonical(
    tmp_path: Path,
) -> None:
    canonical = "data/telegram/2026/08/messages.jsonl"
    shard = tmp_path / "data/telegram/2026/08/messages-001.jsonl"
    shard.parent.mkdir(parents=True)
    first_ingestion = datetime(2026, 8, 22, tzinfo=UTC)
    existing = _record("tg:tawg:1", "old").model_copy(
        update={
            "ingested_at": first_ingestion,
            "source_locator": (
                "repo:data/telegram/2026/08/messages-001.jsonl#tg:tawg:1"
            ),
        }
    )
    shard.write_bytes(JsonlCollection(shard, SourceRecord).merged_bytes([existing]))

    partitions = partition_stable_records(
        tmp_path,
        canonical,
        [_record("tg:tawg:1", "edited"), _record("tg:tawg:2", "new")],
    )

    assert set(partitions) == {
        "data/telegram/2026/08/messages-001.jsonl",
        canonical,
    }
    assert partitions["data/telegram/2026/08/messages-001.jsonl"][0].text_original == (
        "edited"
    )
    assert (
        partitions["data/telegram/2026/08/messages-001.jsonl"][0].ingested_at
        == first_ingestion
    )
    assert partitions[
        "data/telegram/2026/08/messages-001.jsonl"
    ][0].source_locator.endswith("messages-001.jsonl#tg:tawg:1")
    assert [record.record_id for record in partitions[canonical]] == ["tg:tawg:2"]


def test_partition_stable_records_rejects_duplicate_ids_across_shards(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "data/telegram/2026/08"
    parent.mkdir(parents=True)
    for name in ("messages-001.jsonl", "messages-002.jsonl"):
        path = parent / name
        path.write_bytes(
            JsonlCollection(path, SourceRecord).merged_bytes([_record("tg:tawg:1", name)])
        )

    try:
        partition_stable_records(
            tmp_path,
            "data/telegram/2026/08/messages.jsonl",
            [_record("tg:tawg:1", "edited")],
        )
    except CorruptCollection as error:
        assert str(error) == "duplicate record_id across shards: tg:tawg:1"
    else:
        raise AssertionError("duplicate record IDs must fail safely")


def test_partition_stable_records_finds_existing_id_across_months(
    tmp_path: Path,
) -> None:
    old_shard = tmp_path / "data/github/agent-sdk/2026/07/records-001.jsonl"
    old_shard.parent.mkdir(parents=True)
    first_ingestion = datetime(2026, 7, 31, tzinfo=UTC)
    existing = _record("gh:agent-sdk:branch:main", "old").model_copy(
        update={"ingested_at": first_ingestion}
    )
    old_shard.write_bytes(
        JsonlCollection(old_shard, SourceRecord).merged_bytes([existing])
    )

    partitions = partition_stable_records(
        tmp_path,
        "data/github/agent-sdk/2026/08/records.jsonl",
        [_record("gh:agent-sdk:branch:main", "updated")],
        search_relative_root="data/github/agent-sdk",
    )

    assert list(partitions) == ["data/github/agent-sdk/2026/07/records-001.jsonl"]
    assert partitions["data/github/agent-sdk/2026/07/records-001.jsonl"][0].ingested_at == (
        first_ingestion
    )


def test_partition_stable_records_rejects_symlinked_shard(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_bytes(
        JsonlCollection(outside, SourceRecord).merged_bytes([_record("tg:tawg:1", "old")])
    )
    parent = tmp_path / "data/telegram/2026/08"
    parent.mkdir(parents=True)
    (parent / "messages-001.jsonl").symlink_to(outside)

    try:
        partition_stable_records(
            tmp_path,
            "data/telegram/2026/08/messages.jsonl",
            [_record("tg:tawg:1", "edited")],
        )
    except CorruptCollection as error:
        assert str(error) == "unsafe JSONL shard: messages-001.jsonl"
    else:
        raise AssertionError("symlinked shards must fail safely")
