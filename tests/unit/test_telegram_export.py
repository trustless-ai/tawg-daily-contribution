import json
from pathlib import Path

import pytest

from tawg_bot.telegram_export import TelegramDesktopImporter

ROOT = Path(__file__).parents[2]


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
                "name": "TAWG",
                "type": "supergroup",
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
