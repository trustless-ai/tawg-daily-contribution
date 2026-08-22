"""Import sanitized source records from Telegram Desktop JSON exports."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from tawg_bot.aliases import AliasRegistry
from tawg_bot.ids import telegram_id
from tawg_bot.models import AttachmentMetadata, Relation, SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.storage import JsonlCollection
from tawg_bot.unit_of_work import RepositoryUnitOfWork


@dataclass(frozen=True, slots=True)
class TelegramImportResult:
    records: tuple[SourceRecord, ...]
    rejected: int

    @property
    def imported(self) -> int:
        return len(self.records)


class _JsonStream:
    """Small incremental JSON reader for Telegram's top-level export object."""

    def __init__(self, source: TextIO, *, chunk_size: int = 64 * 1024) -> None:
        self.source = source
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def peek(self) -> str:
        self._skip_whitespace()
        while self.position >= len(self.buffer) and not self.eof:
            self._fill()
            self._skip_whitespace()
        return self.buffer[self.position : self.position + 1]

    def consume(self, expected: str) -> None:
        actual = self.peek()
        if actual != expected:
            raise ValueError(f"invalid Telegram JSON: expected {expected!r}, got {actual!r}")
        self.position += 1

    def decode(self) -> Any:
        while True:
            self._skip_whitespace()
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError as error:
                if self.eof:
                    raise ValueError("invalid or truncated Telegram JSON") from error
                self._fill()
                continue
            self.position = end
            return value

    def _skip_whitespace(self) -> None:
        while self.position < len(self.buffer) and self.buffer[self.position].isspace():
            self.position += 1

    def _fill(self) -> None:
        if self.position:
            self.buffer = self.buffer[self.position :]
            self.position = 0
        chunk = self.source.read(self.chunk_size)
        if chunk:
            self.buffer += chunk
        else:
            self.eof = True


class TelegramDesktopImporter:
    def __init__(self, root: Path, privacy: PrivacyFilter, aliases: AliasRegistry) -> None:
        self.root = root
        self.privacy = privacy
        self.aliases = aliases

    @classmethod
    def for_repository(cls, root: Path) -> TelegramDesktopImporter:
        return cls(
            root,
            PrivacyFilter.from_yaml(root / "config/privacy.yml"),
            AliasRegistry.from_yaml(root / "knowledge/meta/aliases.yml"),
        )

    def parse(self, input_path: Path, *, group_slug: str) -> TelegramImportResult:
        records: list[SourceRecord] = []
        rejected = 0
        for message in self._iter_group_messages(input_path):
            if message.get("type") != "message":
                continue
            inspected = self.privacy.inspect(self._flatten_text(message.get("text", "")))
            if not inspected.accepted or inspected.sanitized_text is None:
                rejected += 1
                continue
            message_id = int(message["id"])
            created_at = self._timestamp(message)
            updated_at = self._edited_timestamp(message) or created_at
            display_name = self._safe_display_name(message.get("from"))
            person_id = self.aliases.resolve_telegram_export(
                transient_key=str(message.get("from_id", f"message:{message_id}")),
                display_name=display_name,
            )
            month_path = f"data/telegram/{created_at:%Y/%m}/messages.jsonl"
            record_id = telegram_id(group_slug, message_id)
            relations = []
            if message.get("reply_to_message_id") is not None:
                relations.append(
                    Relation(
                        relation_type="reply_to",
                        target_record_id=telegram_id(
                            group_slug, int(message["reply_to_message_id"])
                        ),
                    )
                )
            attachments = self._attachments(message, bool(inspected.sanitized_text))
            records.append(
                SourceRecord.from_text(
                    record_id=record_id,
                    source_type=SourceType.TELEGRAM_MESSAGE,
                    source_locator=f"repo:{month_path}#{record_id}",
                    author_person_id=person_id,
                    author_source_handle=display_name,
                    created_at=created_at,
                    updated_at=updated_at,
                    text_original=inspected.sanitized_text,
                    relations=relations,
                    attachment_metadata=attachments,
                    ingested_at=datetime.now(UTC),
                    source_payload={"message_kind": "group_message"},
                )
            )
        return TelegramImportResult(tuple(records), rejected)

    @staticmethod
    def _iter_group_messages(input_path: Path) -> Iterator[dict[str, Any]]:
        with input_path.open(encoding="utf-8") as source:
            stream = _JsonStream(source)
            stream.consume("{")
            export_type: object = None
            first_field = True
            saw_messages = False
            while stream.peek() != "}":
                if not first_field:
                    stream.consume(",")
                first_field = False
                key = stream.decode()
                if not isinstance(key, str):
                    raise ValueError("invalid Telegram JSON field name")
                stream.consume(":")
                if key != "messages":
                    value = stream.decode()
                    if key == "type":
                        export_type = value
                    continue
                if saw_messages:
                    raise ValueError("Telegram export contains duplicate messages fields")
                saw_messages = True
                if export_type not in {"group", "supergroup", "private_supergroup"}:
                    raise ValueError("Telegram export is not a recognized group")
                stream.consume("[")
                first_message = True
                while stream.peek() != "]":
                    if not first_message:
                        stream.consume(",")
                    first_message = False
                    message = stream.decode()
                    if not isinstance(message, dict):
                        raise ValueError("invalid Telegram message entry")
                    yield message
                stream.consume("]")
            stream.consume("}")
            if not saw_messages:
                raise ValueError("Telegram export has no messages field")
            if stream.peek():
                raise ValueError("unexpected content after Telegram export")

    def import_file(
        self, input_path: Path, *, group_slug: str, uow: RepositoryUnitOfWork
    ) -> TelegramImportResult:
        result = self.parse(input_path, group_slug=group_slug)
        monthly: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in result.records:
            monthly[f"data/telegram/{record.created_at:%Y/%m}/messages.jsonl"].append(record)
        for path, records in sorted(monthly.items()):
            uow.stage_records(path, self._preserve_first_ingestion(path, records))
        uow.stage_bytes("knowledge/meta/aliases.yml", self.aliases.to_yaml_bytes())
        return result

    def _preserve_first_ingestion(
        self, relative_path: str, records: list[SourceRecord]
    ) -> list[SourceRecord]:
        collection = JsonlCollection(self.root / relative_path, SourceRecord)
        if not collection.path.exists():
            return records
        persisted = collection.decode(collection.path.read_bytes())
        existing = {record.record_id: record for record in persisted}
        return [
            record.model_copy(update={"ingested_at": existing[record.record_id].ingested_at})
            if record.record_id in existing
            else record
            for record in records
        ]

    def _safe_display_name(self, raw_value: object) -> str:
        display_name = str(raw_value) if raw_value else "Unknown member"
        inspected = self.privacy.inspect(display_name)
        if not inspected.accepted or not inspected.sanitized_text:
            return "Unknown member"
        return inspected.sanitized_text

    @staticmethod
    def _flatten_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                item if isinstance(item, str) else str(item.get("text", ""))
                for item in value
                if isinstance(item, str | dict)
            )
        return ""

    @staticmethod
    def _timestamp(message: dict[str, Any]) -> datetime:
        if message.get("date_unixtime") is not None:
            return datetime.fromtimestamp(int(message["date_unixtime"]), tz=UTC)
        return datetime.fromisoformat(str(message["date"])).replace(tzinfo=UTC)

    @staticmethod
    def _edited_timestamp(message: dict[str, Any]) -> datetime | None:
        if not message.get("edited"):
            return None
        return datetime.fromisoformat(str(message["edited"])).replace(tzinfo=UTC)

    @staticmethod
    def _attachments(message: dict[str, Any], has_caption: bool) -> list[AttachmentMetadata]:
        if "photo" in message:
            return [AttachmentMetadata(media_type="photo", has_caption=has_caption)]
        media_type = str(message.get("media_type", ""))
        if media_type in {"video_file", "video_message"} or "video" in message:
            return [AttachmentMetadata(media_type="video", has_caption=has_caption)]
        if "file" in message:
            return [AttachmentMetadata(media_type="file", has_caption=has_caption)]
        return []
