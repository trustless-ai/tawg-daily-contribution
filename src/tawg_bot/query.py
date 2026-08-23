"""Direct, deterministic queries over committed Telegram history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tawg_bot.aliases import AliasError, AliasRegistry
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    record_id: str
    source_locator: str
    created_at: datetime
    updated_at: datetime
    author_person_id: str | None


class TelegramQuery:
    _PATTERNS = ("data/telegram/**/*.jsonl",)

    def __init__(self, root: Path) -> None:
        self.root = root
        aliases_path = root / "knowledge/meta/aliases.yml"
        self.aliases = AliasRegistry.from_yaml(aliases_path) if aliases_path.exists() else None

    def search(
        self,
        *,
        topic: str | None = None,
        person_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[EvidenceHit]:
        if limit <= 0:
            raise ValueError("query limit must be positive")
        for boundary in (start, end):
            if boundary is not None and (
                boundary.tzinfo is None or boundary.utcoffset() != UTC.utcoffset(boundary)
            ):
                raise ValueError("source query time boundaries must use UTC")
        if start is not None and end is not None and start >= end:
            raise ValueError("source query start must precede end")
        topic_term = " ".join(topic.casefold().split()) if topic else None
        matches: list[SourceRecord] = []
        for record in self._records():
            searchable = (
                f"{record.text_original}\n"
                f"{json.dumps(record.source_payload, ensure_ascii=False, sort_keys=True)}"
            ).casefold()
            if topic_term is not None and topic_term not in searchable:
                continue
            if person_id is not None and not self._matches_person(record, person_id):
                continue
            if start is not None and record.updated_at < start:
                continue
            if end is not None and record.updated_at >= end:
                continue
            matches.append(record)
        matches.sort(key=lambda record: (record.created_at, record.record_id))
        return [
            EvidenceHit(
                record_id=record.record_id,
                source_locator=record.source_locator,
                created_at=record.created_at,
                updated_at=record.updated_at,
                author_person_id=record.author_person_id,
            )
            for record in matches[:limit]
        ]

    def records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._records())

    def _records(self) -> list[SourceRecord]:
        resolved_root = self.root.resolve()
        paths = {
            path
            for pattern in self._PATTERNS
            for path in self.root.glob(pattern)
            if path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(resolved_root)
        }
        by_id: dict[str, SourceRecord] = {}
        for path in sorted(paths):
            collection = JsonlCollection(path, SourceRecord)
            for record in collection.decode(path.read_bytes()):
                existing = by_id.get(record.record_id)
                if existing is not None and existing != record:
                    raise ValueError(f"conflicting source record: {record.record_id}")
                by_id[record.record_id] = record
        return list(by_id.values())

    def _matches_person(self, record: SourceRecord, person_id: str) -> bool:
        if record.author_person_id == person_id:
            return True
        if self.aliases is None or record.author_source_handle is None:
            return False
        source = self._identity_source(record.source_type)
        if source is None:
            return False
        if source == "telegram":
            if record.author_source_handle.startswith("@"):
                try:
                    return (
                        self.aliases.lookup_public_handle(source, record.author_source_handle)
                        == person_id
                    )
                except AliasError:
                    return False
            try:
                display_owner = self.aliases.lookup_display_name(record.author_source_handle)
            except AliasError:
                return False
            return display_owner == person_id
        try:
            return (
                self.aliases.lookup_public_handle(source, record.author_source_handle) == person_id
            )
        except AliasError:
            return False

    @staticmethod
    def _identity_source(source_type: SourceType) -> str | None:
        if source_type is SourceType.TELEGRAM_MESSAGE:
            return "telegram"
        return None


SourceQuery = TelegramQuery
