"""Recoverable multi-file publication for one repository operation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tawg_bot.models import SourceRecord
from tawg_bot.storage import JsonlCollection

_OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ConcurrentModification(RuntimeError):
    """Raised when a staged target changes before publication."""


@dataclass(frozen=True, slots=True)
class PublishResult:
    operation_id: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StagedWrite:
    relative_path: str
    target: Path
    prepared: Path
    expected_sha256: str | None
    content_sha256: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _path_hash(path: Path) -> str | None:
    return _sha256(path.read_bytes()) if path.exists() else None


class RepositoryUnitOfWork:
    def __init__(self, root: Path, *, operation_id: str) -> None:
        if not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("invalid operation_id")
        self.root = root.resolve()
        self.operation_id = operation_id
        self.transaction_dir = self.root / ".local" / "transactions" / operation_id
        self._writes: dict[str, _StagedWrite] = {}

    def stage_records(self, relative_path: str, records: Iterable[SourceRecord]) -> None:
        target = self._target(relative_path)
        payload = JsonlCollection(target, SourceRecord).merged_bytes(records)
        self.stage_bytes(relative_path, payload)

    def stage_json(self, relative_path: str, value: Mapping[str, Any] | list[Any]) -> None:
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self.stage_bytes(relative_path, payload)

    def stage_bytes(self, relative_path: str, payload: bytes) -> None:
        if relative_path in self._writes:
            raise ValueError(f"path already staged: {relative_path}")
        target = self._target(relative_path)
        prepared = self.transaction_dir / "prepared" / relative_path
        prepared.parent.mkdir(parents=True, exist_ok=True)
        prepared.write_bytes(payload)
        self._writes[relative_path] = _StagedWrite(
            relative_path=relative_path,
            target=target,
            prepared=prepared,
            expected_sha256=_path_hash(target),
            content_sha256=_sha256(payload),
        )

    def publish(self) -> PublishResult:
        ordered = list(self._writes.values())
        for write in ordered:
            if _path_hash(write.target) != write.expected_sha256:
                self._cleanup()
                raise ConcurrentModification(f"target changed: {write.relative_path}")

        changed = [write for write in ordered if write.content_sha256 != write.expected_sha256]
        if not changed:
            self._cleanup()
            return PublishResult(self.operation_id, ())

        backups: dict[str, Path | None] = {}
        published: list[_StagedWrite] = []
        try:
            for write in changed:
                backup: Path | None = None
                if write.target.exists():
                    backup = self.transaction_dir / "backups" / write.relative_path
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(write.target, backup)
                backups[write.relative_path] = backup
                write.target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(write.prepared, write.target)
                published.append(write)
        except BaseException:
            for write in reversed(published):
                backup = backups[write.relative_path]
                if backup is None:
                    write.target.unlink(missing_ok=True)
                else:
                    os.replace(backup, write.target)
            self._cleanup()
            raise

        self._cleanup()
        return PublishResult(self.operation_id, tuple(write.relative_path for write in changed))

    def _target(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise ValueError("target path must be a confined relative path")
        target = self.root.joinpath(candidate)
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise ValueError("target path escapes repository root")
        return target

    def _cleanup(self) -> None:
        if self.transaction_dir.exists():
            shutil.rmtree(self.transaction_dir)
