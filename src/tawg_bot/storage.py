"""Canonical JSONL encoding and stable-ID upserts."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel


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
