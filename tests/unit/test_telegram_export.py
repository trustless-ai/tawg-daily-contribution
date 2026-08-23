import gc
import json
import tracemalloc
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from tawg_bot.telegram_export import TelegramDesktopImporter, _JsonStream

ROOT = Path(__file__).parents[2]


def test_json_stream_skip_value_handles_number_split_across_chunks() -> None:
    stream = _JsonStream(
        StringIO('{"value":12345678901234567890,"next":1}'),
        chunk_size=16,
    )

    stream.skip_value()

    assert stream.peek() == ""


def test_export_parser_normalizes_messages_without_ids_or_media_paths() -> None:
    importer = TelegramDesktopImporter.for_repository(ROOT)
    result = importer.parse(ROOT / "tests/fixtures/telegram_export.json", group_slug="tawg")

    assert [record.record_id for record in result.records] == [
        "tg:tawg:1",
        "tg:tawg:2",
        "tg:tawg:3",
        "tg:tawg:4",
        "tg:tawg:6",
    ]
    assert result.records[0].text_original == "Parser shipped. Contact [REDACTED_EMAIL]"
    assert result.records[1].text_original == "Review ERC-8004"
    assert result.records[2].relations[0].target_record_id == "tg:tawg:2"
    assert result.records[2].updated_at == datetime(2026, 8, 22, 15, 20, tzinfo=UTC)
    assert result.records[2].attachment_metadata[0].media_type == "photo"
    assert result.records[3].attachment_metadata[0].media_type == "video"
    serialized = json.dumps([record.model_dump(mode="json") for record in result.records])
    assert "user123456" not in serialized
    assert "photo_1.jpg" not in serialized
    assert "demo.mp4" not in serialized


def test_colliding_display_names_get_distinct_local_person_ids() -> None:
    importer = TelegramDesktopImporter.for_repository(ROOT)
    result = importer.parse(ROOT / "tests/fixtures/telegram_export.json", group_slug="tawg")

    assert result.records[0].author_person_id == "alice"
    assert result.records[1].author_person_id == "alice-2"


def test_export_parser_does_not_load_the_whole_export(monkeypatch) -> None:
    importer = TelegramDesktopImporter.for_repository(ROOT)

    def forbid_read_text(*args, **kwargs):
        raise AssertionError("history importer must stream the export")

    monkeypatch.setattr(Path, "read_text", forbid_read_text)

    assert importer.parse(
        ROOT / "tests/fixtures/telegram_export.json", group_slug="tawg"
    ).imported == 5


def test_export_message_iterator_has_bounded_memory(tmp_path: Path) -> None:
    export = tmp_path / "large-container.json"
    messages = [
        {
            "id": index,
            "type": "message",
            "date": "2026-08-22T23:00:00",
            "from": "Alice",
            "from_id": "user123",
            "text": "x" * 32_768,
        }
        for index in range(128)
    ]
    export.write_text(
        json.dumps(
            {
                "chats": {
                    "list": [
                        {
                            "name": "trustless-ai",
                            "type": "private_supergroup",
                            "id": 4384669042,
                            "messages": messages,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    del messages
    gc.collect()
    importer = TelegramDesktopImporter.for_repository(ROOT)

    tracemalloc.start()
    iterator = importer._iter_group_messages(export)
    first = next(iterator)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert first["id"] == 0
    assert peak < 1_000_000


def test_export_validation_and_streaming_share_one_file_handle(monkeypatch) -> None:
    valid = json.dumps(
        {
            "name": "trustless-ai",
            "type": "private_supergroup",
            "id": 4384669042,
            "messages": [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-08-22T23:00:00",
                    "from": "Alice",
                    "from_id": "user123",
                    "text": "validated",
                }
            ],
        }
    )
    replacement = json.dumps(
        {
            "name": "Unrelated group",
            "type": "private_supergroup",
            "id": 999999,
            "messages": [
                {
                    "id": 999,
                    "type": "message",
                    "date": "2026-08-22T23:00:00",
                    "from": "Mallory",
                    "from_id": "user999",
                    "text": "replacement",
                }
            ],
        }
    )
    handles = iter((StringIO(valid), StringIO(replacement)))
    open_count = 0

    def replaced_open(*args, **kwargs):
        nonlocal open_count
        open_count += 1
        return next(handles)

    importer = TelegramDesktopImporter.for_repository(ROOT)
    monkeypatch.setattr(Path, "open", replaced_open)

    result = importer.parse(Path("ignored.json"), group_slug="tawg")

    assert [record.record_id for record in result.records] == ["tg:tawg:1"]
    assert open_count == 1


def test_export_streams_from_immutable_snapshot(monkeypatch) -> None:
    valid = json.dumps(
        {
            "name": "trustless-ai",
            "type": "private_supergroup",
            "id": 4384669042,
            "messages": [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-08-22T23:00:00",
                    "from": "Alice",
                    "from_id": "user123",
                    "text": "validated",
                }
            ],
        }
    )
    replacement = json.dumps(
        {
            "name": "Unrelated group",
            "type": "private_supergroup",
            "id": 999999,
            "messages": [
                {
                    "id": 999,
                    "type": "message",
                    "date": "2026-08-22T23:00:00",
                    "from": "Mallory",
                    "from_id": "user999",
                    "text": "replacement",
                }
            ],
        }
    )

    class MutatingExport(StringIO):
        def seek(self, offset, whence=0):
            if offset == 0 and whence == 0 and self.tell() != 0:
                self.truncate(0)
                super().seek(0)
                self.write(replacement)
            return super().seek(offset, whence)

    importer = TelegramDesktopImporter.for_repository(ROOT)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: MutatingExport(valid))

    result = importer.parse(Path("mutable.json"), group_slug="tawg")

    assert [record.record_id for record in result.records] == ["tg:tawg:1"]


def test_export_parser_accepts_single_group_chats_container(tmp_path: Path) -> None:
    export = tmp_path / "container.json"
    export.write_text(
        json.dumps(
            {
                "about": "Telegram Desktop export",
                "chats": {
                    "list": [
                        {
                            "name": "trustless-ai",
                            "type": "private_supergroup",
                            "id": 4384669042,
                            "messages": [
                                {
                                    "id": 42,
                                    "type": "message",
                                    "date": "2026-08-22T23:00:00",
                                    "from": "Alice",
                                    "from_id": "user123",
                                    "text": "Container export works",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)

    result = importer.parse(export, group_slug="tawg")

    assert [record.record_id for record in result.records] == ["tg:tawg:42"]
    assert result.records[0].text_original == "Container export works"


def test_export_parser_rejects_multiple_group_chats_container(tmp_path: Path) -> None:
    export = tmp_path / "multiple-groups.json"
    export.write_text(
        json.dumps(
            {
                "chats": {
                    "list": [
                        {
                            "name": "trustless-ai",
                            "type": "private_supergroup",
                            "id": 4384669042,
                            "messages": [
                                {
                                    "id": 1,
                                    "type": "message",
                                    "date": "2026-08-22T23:00:00",
                                    "from": "Must not persist",
                                    "from_id": "user-rejected",
                                    "text": "discard me",
                                }
                            ],
                        },
                        {
                            "name": "Another group",
                            "type": "supergroup",
                            "messages": [],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)
    aliases_before = importer.aliases.to_yaml_bytes()

    with pytest.raises(ValueError, match="exactly one group"):
        importer.parse(export, group_slug="tawg")
    assert importer.aliases.to_yaml_bytes() == aliases_before


def test_export_parser_rejects_wrong_group_identity(tmp_path: Path) -> None:
    export = tmp_path / "wrong-group.json"
    export.write_text(
        json.dumps(
            {
                "name": "trustless-ai",
                "type": "private_supergroup",
                "id": 999999,
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)
    importer.expected_group_name = "trustless-ai"

    with pytest.raises(ValueError, match="identity does not match"):
        importer.parse(export, group_slug="tawg")


def test_export_parser_accepts_reordered_chat_fields(tmp_path: Path) -> None:
    export = tmp_path / "reordered.json"
    export.write_text(
        '{"chats":{"list":[{"messages":[],"id":4384669042,'
        '"type":"private_supergroup","name":"trustless-ai"}]}}',
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)
    importer.expected_group_name = "trustless-ai"

    assert importer.parse(export, group_slug="tawg").imported == 0


def test_export_parser_rejects_private_chats(tmp_path: Path) -> None:
    export = tmp_path / "private.json"
    export.write_text(
        json.dumps({"name": "Private", "type": "personal_chat", "messages": []}),
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)

    with pytest.raises(ValueError, match="not a recognized group"):
        importer.parse(export, group_slug="tawg")


def test_export_parser_sanitizes_personal_data_in_display_names(tmp_path: Path) -> None:
    export = tmp_path / "group.json"
    export.write_text(
        json.dumps(
            {
                "name": "trustless-ai",
                "type": "supergroup",
                "id": 4384669042,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-08-22T23:00:00",
                        "from": "private@example.com",
                        "from_id": "user123",
                        "text": "hello",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    importer = TelegramDesktopImporter.for_repository(ROOT)

    result = importer.parse(export, group_slug="tawg")

    serialized = json.dumps(result.records[0].model_dump(mode="json"))
    assert "private@example.com" not in serialized
    assert result.records[0].author_source_handle == "[REDACTED_EMAIL]"
