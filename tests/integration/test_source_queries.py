from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.aliases import AliasRegistry
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.query import SourceQuery
from tawg_bot.storage import JsonlCollection


def record(
    record_id: str,
    source_type: SourceType,
    person_id: str,
    created_at: datetime,
    text: str,
    locator: str,
) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=source_type,
        source_locator=locator,
        author_person_id=person_id,
        author_source_handle=person_id,
        created_at=created_at,
        updated_at=created_at,
        text_original=text,
        ingested_at=created_at,
    )


def seed_sources(root: Path) -> None:
    records = [
        record(
            "tg:tawg:1",
            SourceType.TELEGRAM_MESSAGE,
            "alice",
            datetime(2026, 8, 22, 23, 10, tzinfo=UTC),
            "I reviewed ERC-8004 validation semantics.",
            "repo:data/telegram/2026/08/messages.jsonl#tg:tawg:1",
        ),
        record(
            "gh:agent-ercs:commit:abc",
            SourceType.GITHUB_COMMIT,
            "alice",
            datetime(2026, 8, 23, 0, 20, tzinfo=UTC),
            "Clarify ERC-8183 settlement invariants.",
            "https://github.com/trustless-ai/agent-ercs/commit/abc",
        ),
        record(
            "magicians:25098:post:3",
            SourceType.MAGICIANS_POST,
            "bob",
            datetime(2026, 8, 23, 1, 10, tzinfo=UTC),
            "ERC-8004 should retain on-chain reads.",
            "https://ethereum-magicians.org/t/erc-8004-trustless-agents/25098/3",
        ),
    ]
    paths = [
        root / "data/telegram/2026/08/messages.jsonl",
        root / "data/github/agent-ercs/2026/08/records.jsonl",
        root / "data/magicians/2026/08/posts.jsonl",
    ]
    for path, item in zip(paths, records, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes([item]))


def test_queries_use_telegram_only_even_when_legacy_external_mirrors_exist(
    tmp_path: Path,
) -> None:
    seed_sources(tmp_path)
    query = SourceQuery(tmp_path)

    topic_hits = query.search(topic="erc-8004")
    person_hits = query.search(person_id="alice")
    time_hits = query.search(
        start=datetime(2026, 8, 23, 0, 0, tzinfo=UTC),
        end=datetime(2026, 8, 23, 1, 0, tzinfo=UTC),
    )

    assert [hit.record_id for hit in topic_hits] == ["tg:tawg:1"]
    assert [hit.record_id for hit in person_hits] == ["tg:tawg:1"]
    assert time_hits == []
    assert {record.source_type for record in query.records()} == {SourceType.TELEGRAM_MESSAGE}


def test_time_query_rejects_non_utc_boundaries(tmp_path: Path) -> None:
    seed_sources(tmp_path)

    with pytest.raises(ValueError, match="UTC"):
        SourceQuery(tmp_path).search(start=datetime(2026, 8, 23))


def test_person_query_does_not_resolve_legacy_external_handle_aliases(tmp_path: Path) -> None:
    seed_sources(tmp_path)
    aliases = AliasRegistry()
    person_id = aliases.resolve_public_handle(
        source="telegram", public_handle="alice_tg", display_name="Alice Local"
    )
    aliases.add_public_handle(person_id, source="github", public_handle="alice")
    aliases_path = tmp_path / "knowledge/meta/aliases.yml"
    aliases_path.parent.mkdir(parents=True)
    aliases_path.write_bytes(aliases.to_yaml_bytes())

    hits = SourceQuery(tmp_path).search(person_id=person_id)

    assert hits == []


def test_person_query_accepts_telegram_export_display_names(tmp_path: Path) -> None:
    created_at = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    path = tmp_path / "data/telegram/2026/08/messages.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        SourceRecord.from_text(
            record_id="tg:tawg:10",
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:10",
            author_person_id="damon-zwicker",
            author_source_handle="Damon Zwicker",
            created_at=created_at,
            updated_at=created_at,
            text_original="First message.",
            ingested_at=created_at,
        ),
        SourceRecord.from_text(
            record_id="tg:tawg:11",
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:11",
            author_person_id="baby-blue-viper",
            author_source_handle="Baby Blue Viper",
            created_at=created_at,
            updated_at=created_at,
            text_original="Second message.",
            ingested_at=created_at,
        ),
    ]
    path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes(records))
    aliases = AliasRegistry()
    aliases.resolve_telegram_export(transient_key="damon", display_name="Damon Zwicker")
    aliases.resolve_telegram_export(transient_key="viper", display_name="Baby Blue Viper")
    aliases_path = tmp_path / "knowledge/meta/aliases.yml"
    aliases_path.parent.mkdir(parents=True)
    aliases_path.write_bytes(aliases.to_yaml_bytes())

    hits = SourceQuery(tmp_path).search(person_id="baby-blue-viper")

    assert [hit.record_id for hit in hits] == ["tg:tawg:11"]


def test_person_query_does_not_treat_export_name_as_live_username(tmp_path: Path) -> None:
    created_at = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    path = tmp_path / "data/telegram/2026/08/messages.jsonl"
    path.parent.mkdir(parents=True)
    historical = SourceRecord.from_text(
        record_id="tg:tawg:12",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:12",
        author_person_id="historical-alice",
        author_source_handle="alice",
        created_at=created_at,
        updated_at=created_at,
        text_original="Historical message.",
        ingested_at=created_at,
    )
    path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes([historical]))
    aliases = AliasRegistry()
    live_person = aliases.resolve_public_handle(
        source="telegram", public_handle="alice", display_name="Alice Live"
    )
    aliases_path = tmp_path / "knowledge/meta/aliases.yml"
    aliases_path.parent.mkdir(parents=True)
    aliases_path.write_bytes(aliases.to_yaml_bytes())

    hits = SourceQuery(tmp_path).search(person_id=live_person)

    assert hits == []
