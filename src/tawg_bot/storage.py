"""Canonical JSONL encoding and stable-ID upserts."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from tawg_bot.models import SourceRecord


class CorruptCollection(ValueError):
    """Raised when persisted JSONL violates the collection contract."""


class JsonlCollection[ModelT: BaseModel]:
    def __init__(self, path: Path, model_type: type[ModelT]) -> None:
        self.path = path
        self.model_type = model_type

    def decode(self, payload: bytes) -> list[ModelT]:
        records: list[ModelT] = []
        seen: set[str] = set()
        for line_number, line in enumerate(payload.splitlines(), start=1):
            if not line.strip():
                continue
            record = self.model_type.model_validate_json(line)
            record_id = record.model_dump(include={"record_id"}).get("record_id")
            if not isinstance(record_id, str):
                raise CorruptCollection(f"line {line_number} has no string record_id")
            if record_id in seen:
                raise CorruptCollection(f"duplicate record_id: {record_id}")
            seen.add(record_id)
            records.append(record)
        return records

    def merged_bytes(self, incoming: Iterable[ModelT]) -> bytes:
        current = self.decode(self.path.read_bytes()) if self.path.exists() else []
        by_id = {self._record_id(record): record for record in current}
        for record in incoming:
            record_id = self._record_id(record)
            by_id[record_id] = record
        lines = [
            json.dumps(
                by_id[record_id].model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for record_id in sorted(by_id)
        ]
        return (("\n".join(lines) + "\n") if lines else "").encode()

    @staticmethod
    def _record_id(record: ModelT) -> str:
        record_id = record.model_dump(include={"record_id"}).get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("record_id must be a non-empty string")
        return record_id


def partition_stable_records(
    root: Path,
    canonical_relative_path: str,
    incoming: Iterable[SourceRecord],
    *,
    search_relative_root: str | None = None,
) -> dict[str, list[SourceRecord]]:
    """Route updates to their existing JSONL shard and new IDs to the canonical file."""
    root = root.resolve()
    canonical_relative = Path(canonical_relative_path)
    if (
        canonical_relative.is_absolute()
        or ".." in canonical_relative.parts
        or canonical_relative.suffix != ".jsonl"
    ):
        raise ValueError("canonical path must be a confined JSONL path")
    canonical = root / canonical_relative
    if not canonical.resolve(strict=False).is_relative_to(root):
        raise ValueError("canonical path must stay inside the repository")

    search_relative = (
        Path(search_relative_root)
        if search_relative_root is not None
        else canonical_relative.parent
    )
    if search_relative.is_absolute() or ".." in search_relative.parts:
        raise ValueError("search root must be a confined repository path")
    search_root = root / search_relative
    if not search_root.resolve(strict=False).is_relative_to(root):
        raise ValueError("search root must stay inside the repository")

    existing: dict[str, tuple[SourceRecord, str]] = {}
    stem = canonical.stem
    candidates = (
        sorted(search_root.rglob(f"{stem}*.jsonl")) if search_root.exists() else []
    )
    for path in candidates:
        if path.name != canonical.name and not path.name.startswith(f"{stem}-"):
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            raise CorruptCollection(f"unsafe JSONL shard: {path.name}")
        relative_path = path.relative_to(root).as_posix()
        for record in JsonlCollection(path, SourceRecord).decode(path.read_bytes()):
            if record.record_id in existing:
                raise CorruptCollection(
                    f"duplicate record_id across shards: {record.record_id}"
                )
            existing[record.record_id] = (record, relative_path)

    partitions: dict[str, list[SourceRecord]] = {}
    for record in incoming:
        persisted = existing.get(record.record_id)
        if persisted is None:
            target = canonical_relative.as_posix()
            stable = record
        else:
            previous, target = persisted
            stable_fields: dict[str, object] = {"ingested_at": previous.ingested_at}
            if previous.source_locator.startswith("repo:"):
                stable_fields["source_locator"] = previous.source_locator
            stable = record.model_copy(update=stable_fields)
        partitions.setdefault(target, []).append(stable)
    return partitions
