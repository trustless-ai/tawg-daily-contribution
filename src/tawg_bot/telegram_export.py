"""Import sanitized source records from Telegram Desktop JSON exports."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import yaml

from tawg_bot.aliases import AliasRegistry
from tawg_bot.ids import telegram_id
from tawg_bot.models import AttachmentMetadata, Relation, SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.storage import partition_stable_records
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
            if end == len(self.buffer) and not self.eof:
                self._fill()
                continue
            self.position = end
            return value

    def skip_value(self) -> None:
        marker = self.peek()
        if marker == "{":
            self.consume("{")
            first_field = True
            while self.peek() != "}":
                if not first_field:
                    self.consume(",")
                first_field = False
                if not isinstance(self.decode(), str):
                    raise ValueError("invalid JSON object field name")
                self.consume(":")
                self.skip_value()
            self.consume("}")
            return
        if marker == "[":
            self.consume("[")
            first_item = True
            while self.peek() != "]":
                if not first_item:
                    self.consume(",")
                first_item = False
                self.skip_value()
            self.consume("]")
            return
        self.decode()

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
    def __init__(
        self,
        root: Path,
        privacy: PrivacyFilter,
        aliases: AliasRegistry,
        *,
        expected_group_name: str,
        expected_group_id: int,
    ) -> None:
        self.root = root
        self.privacy = privacy
        self.aliases = aliases
        self.expected_group_name = self._normalize_group_name(expected_group_name)
        self.expected_group_id = expected_group_id

    @classmethod
    def for_repository(cls, root: Path) -> TelegramDesktopImporter:
        sources = yaml.safe_load((root / "config/sources.yml").read_text(encoding="utf-8"))
        telegram = sources.get("telegram") if isinstance(sources, dict) else None
        expected_group_name = (
            telegram.get("expected_export_name") if isinstance(telegram, dict) else None
        )
        expected_group_id = (
            telegram.get("expected_export_id") if isinstance(telegram, dict) else None
        )
        if not isinstance(expected_group_name, str) or not expected_group_name.strip():
            raise ValueError("expected Telegram export group name is not configured")
        if not isinstance(expected_group_id, int):
            raise ValueError("expected Telegram export group ID is not configured")
        return cls(
            root,
            PrivacyFilter.from_yaml(root / "config/privacy.yml"),
            AliasRegistry.from_yaml(root / "knowledge/meta/aliases.yml"),
            expected_group_name=expected_group_name,
            expected_group_id=expected_group_id,
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

    def _iter_group_messages(self, input_path: Path) -> Iterator[dict[str, Any]]:
        with (
            input_path.open(encoding="utf-8") as source,
            tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as snapshot,
        ):
            shutil.copyfileobj(source, snapshot, length=64 * 1024)
            snapshot.seek(0)
            self._validate_group_export(_JsonStream(snapshot))
            snapshot.seek(0)
            yield from self._stream_group_messages(_JsonStream(snapshot))

    def _validate_group_export(self, stream: _JsonStream) -> None:
        stream.consume("{")
        export_type: object = None
        export_name: object = None
        export_id: object = None
        first_field = True
        message_source: str | None = None
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram JSON field name")
            stream.consume(":")
            if key not in {"chats", "messages"}:
                value = stream.decode()
                if key == "type":
                    export_type = value
                elif key == "name":
                    export_name = value
                elif key == "id":
                    export_id = value
                continue
            if message_source is not None:
                raise ValueError("Telegram export contains duplicate message sources")
            message_source = key
            if key == "chats":
                self._validate_chats_container(stream)
            else:
                stream.skip_value()
        stream.consume("}")
        if message_source is None:
            raise ValueError("Telegram export has no messages field")
        if message_source == "messages":
            self._require_group_identity(export_type, export_name, export_id)
        if stream.peek():
            raise ValueError("unexpected content after Telegram export")

    def _validate_chats_container(self, stream: _JsonStream) -> None:
        stream.consume("{")
        first_field = True
        saw_list = False
        chat_count = 0
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram chats field name")
            stream.consume(":")
            if key != "list":
                stream.decode()
                continue
            if saw_list:
                raise ValueError("Telegram export contains duplicate chat lists")
            saw_list = True
            stream.consume("[")
            first_chat = True
            while stream.peek() != "]":
                if not first_chat:
                    stream.consume(",")
                first_chat = False
                chat_count += 1
                if chat_count > 1:
                    raise ValueError("Telegram export must contain exactly one group")
                self._validate_chat(stream)
            stream.consume("]")
        stream.consume("}")
        if not saw_list or chat_count != 1:
            raise ValueError("Telegram export must contain exactly one group")

    def _validate_chat(self, stream: _JsonStream) -> None:
        stream.consume("{")
        export_type: object = None
        export_name: object = None
        export_id: object = None
        first_field = True
        saw_messages = False
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram chat field name")
            stream.consume(":")
            if key != "messages":
                value = stream.decode()
                if key == "type":
                    export_type = value
                elif key == "name":
                    export_name = value
                elif key == "id":
                    export_id = value
                continue
            if saw_messages:
                raise ValueError("Telegram export contains duplicate messages fields")
            saw_messages = True
            stream.skip_value()
        stream.consume("}")
        if not saw_messages:
            raise ValueError("Telegram group export has no messages field")
        self._require_group_identity(export_type, export_name, export_id)

    def _stream_group_messages(self, stream: _JsonStream) -> Iterator[dict[str, Any]]:
        stream.consume("{")
        first_field = True
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram JSON field name")
            stream.consume(":")
            if key == "messages":
                yield from self._iter_messages_array(stream)
            elif key == "chats":
                yield from self._stream_chats_container(stream)
            else:
                stream.skip_value()
        stream.consume("}")

    def _stream_chats_container(self, stream: _JsonStream) -> Iterator[dict[str, Any]]:
        stream.consume("{")
        first_field = True
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram chats field name")
            stream.consume(":")
            if key != "list":
                stream.skip_value()
                continue
            stream.consume("[")
            yield from self._stream_chat(stream)
            stream.consume("]")
        stream.consume("}")

    def _stream_chat(self, stream: _JsonStream) -> Iterator[dict[str, Any]]:
        stream.consume("{")
        first_field = True
        while stream.peek() != "}":
            if not first_field:
                stream.consume(",")
            first_field = False
            key = stream.decode()
            if not isinstance(key, str):
                raise ValueError("invalid Telegram chat field name")
            stream.consume(":")
            if key == "messages":
                yield from self._iter_messages_array(stream)
            else:
                stream.skip_value()
        stream.consume("}")

    @staticmethod
    def _iter_messages_array(stream: _JsonStream) -> Iterator[dict[str, Any]]:
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

    def _require_group_identity(
        self, export_type: object, export_name: object, export_id: object
    ) -> None:
        if export_type not in {"group", "supergroup", "private_supergroup"}:
            raise ValueError("Telegram export is not a recognized group")
        if (
            not isinstance(export_name, str)
            or self._normalize_group_name(export_name) != self.expected_group_name
            or export_id != self.expected_group_id
        ):
            raise ValueError("Telegram export group identity does not match configuration")

    @staticmethod
    def _normalize_group_name(value: str) -> str:
        normalized = " ".join(value.casefold().split())
        if not normalized:
            raise ValueError("Telegram export group name cannot be empty")
        return normalized

    def import_file(
        self, input_path: Path, *, group_slug: str, uow: RepositoryUnitOfWork
    ) -> TelegramImportResult:
        uow.register_external_evidence(())
        result = self.parse(input_path, group_slug=group_slug)
        monthly: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in result.records:
            monthly[f"data/telegram/{record.created_at:%Y/%m}/messages.jsonl"].append(record)
        for path, records in sorted(monthly.items()):
            partitions = partition_stable_records(
                self.root,
                path,
                records,
                search_relative_root="data/telegram",
            )
            for target, stable_records in sorted(partitions.items()):
                uow.stage_records(target, stable_records)
        uow.stage_bytes("knowledge/meta/aliases.yml", self.aliases.to_yaml_bytes())
        return result

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
        if message.get("edited_unixtime") is not None:
            return datetime.fromtimestamp(int(message["edited_unixtime"]), tz=UTC)
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
