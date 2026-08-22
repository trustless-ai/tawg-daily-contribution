"""Inspect-then-apply transactions confined to canonical knowledge files."""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from tawg_bot.models import StrictModel
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter, parse_frontmatter


class TransactionRejected(ValueError):
    """Raised when a proposed canonical write violates deterministic policy."""


class ApprovalMismatch(TransactionRejected):
    """Raised when apply is not bound to the current inspected transaction."""


class VaultWrite(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    expected_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=1_048_576)
    citations: list[str] = Field()

    @field_validator("citations")
    @classmethod
    def citations_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("citations must be unique")
        return value


class VaultTransaction(StrictModel):
    schema_version: Literal["tawg.vault-transaction.v1"] = "tawg.vault-transaction.v1"
    operation_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    writes: list[VaultWrite] = Field(min_length=1, max_length=64)


@dataclass(frozen=True, slots=True)
class Inspection:
    approval_sha256: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    changed_paths: tuple[str, ...]


class VaultTransactionEngine:
    _ALLOWED_SUFFIXES = frozenset({".md", ".json", ".yml", ".yaml"})
    _MAX_TOTAL_BYTES = 1024 * 1024

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    def inspect(self, transaction: VaultTransaction) -> Inspection:
        writes = self._resolve_writes(transaction)
        known_sources = {record.record_id: record for record in SourceQuery(self.root).records()}
        total_bytes = 0
        overrides: dict[str, bytes] = {}
        changed: list[str] = []
        expected: dict[str, str | None] = {}
        for write, target in writes:
            payload = write.content.encode("utf-8")
            total_bytes += len(payload)
            if total_bytes > self._MAX_TOTAL_BYTES:
                raise TransactionRejected("transaction exceeds 1 MiB of canonical text")
            actual = self._path_hash(target)
            if write.expected_sha256 != actual:
                raise TransactionRejected(f"expected hash mismatch: {write.path}")
            expected[write.path] = actual
            try:
                self.privacy.assert_public(write.content)
            except PrivacyViolation as error:
                raise TransactionRejected(f"privacy rejection: {error}") from None
            if target.suffix.casefold() == ".md":
                self._validate_citations(write, known_sources)
            overrides[write.path] = payload
            if actual != hashlib.sha256(payload).hexdigest():
                changed.append(write.path)

        report = VaultLinter(self.root).lint(overrides=overrides)
        errors = [finding for finding in report.findings if finding.severity == "error"]
        if errors:
            first = errors[0]
            raise TransactionRejected(f"vault lint {first.category}: {first.path}: {first.message}")

        canonical = json.dumps(
            {
                "root": self.root.as_posix(),
                "transaction": transaction.model_dump(mode="json"),
                "expected": expected,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return Inspection(hashlib.sha256(canonical).hexdigest(), tuple(sorted(changed)))

    def apply(self, transaction: VaultTransaction, approval_sha256: str) -> ApplyResult:
        uow = RepositoryUnitOfWork(self.root, operation_id=transaction.operation_id)
        self.stage(transaction, approval_sha256, uow)
        result = uow.publish()
        return ApplyResult(result.changed_paths)

    def stage(
        self,
        transaction: VaultTransaction,
        approval_sha256: str,
        uow: RepositoryUnitOfWork,
    ) -> None:
        if uow.root != self.root:
            raise TransactionRejected("vault transaction and unit of work roots differ")
        inspection = self.inspect(transaction)
        if not hmac.compare_digest(inspection.approval_sha256, approval_sha256):
            raise ApprovalMismatch("approval hash does not match the current inspection")
        for write in transaction.writes:
            uow.stage_bytes(write.path, write.content.encode("utf-8"))

    def _resolve_writes(self, transaction: VaultTransaction) -> list[tuple[VaultWrite, Path]]:
        existing_case = {
            self._path_key(path.relative_to(self.root).as_posix()): (
                path.relative_to(self.root).as_posix()
            )
            for path in (self.root / "knowledge").rglob("*")
            if path.is_file() or path.is_symlink()
        }
        seen: dict[str, str] = {}
        resolved: list[tuple[VaultWrite, Path]] = []
        for write in transaction.writes:
            if "\\" in write.path:
                raise TransactionRejected("vault paths must use forward slashes")
            relative = PurePosixPath(write.path)
            normalized = relative.as_posix()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or normalized != write.path
                or any(ord(character) < 32 for character in write.path)
                or len(relative.parts) < 2
                or relative.parts[0] != "knowledge"
                or any(part.startswith(".") for part in relative.parts[1:])
                or relative.suffix.casefold() not in self._ALLOWED_SUFFIXES
            ):
                raise TransactionRejected(
                    f"write path is outside canonical knowledge: {write.path}"
                )
            folded = self._path_key(normalized)
            if folded in seen:
                raise TransactionRejected(
                    f"duplicate or case-colliding writes: {seen[folded]} and {normalized}"
                )
            seen[folded] = normalized
            collision = existing_case.get(folded)
            if collision is not None and collision != normalized:
                raise TransactionRejected(f"case-colliding path: {normalized}")
            target = self.root.joinpath(*relative.parts)
            if target.exists() and not target.is_file():
                raise TransactionRejected(f"vault write target is not a file: {normalized}")
            if not target.resolve(strict=False).is_relative_to(self.root / "knowledge"):
                raise TransactionRejected(f"write path escapes canonical knowledge: {normalized}")
            cursor = target
            while cursor != self.root:
                if cursor.is_symlink():
                    raise TransactionRejected(f"symlinked vault path is forbidden: {normalized}")
                if cursor != target and cursor.exists() and not cursor.is_dir():
                    raise TransactionRejected(f"vault parent is not a directory: {normalized}")
                cursor = cursor.parent
            resolved.append((write, target))
        return resolved

    @staticmethod
    def _validate_citations(
        write: VaultWrite, known_sources: Mapping[str, object]
    ) -> None:
        if not write.citations:
            raise TransactionRejected(f"Markdown write has no citations: {write.path}")
        unknown = [source_id for source_id in write.citations if source_id not in known_sources]
        if unknown:
            raise TransactionRejected(f"unknown citation: {unknown[0]}")
        frontmatter, _ = parse_frontmatter(write.content)
        source_ids = frontmatter.get("source_ids") if frontmatter is not None else None
        if (
            not isinstance(source_ids, list)
            or not all(isinstance(source_id, str) for source_id in source_ids)
            or not set(write.citations).issubset(source_ids)
        ):
            raise TransactionRejected(f"Markdown frontmatter omits citations: {write.path}")
        unknown_frontmatter = [
            source_id for source_id in source_ids if source_id not in known_sources
        ]
        if unknown_frontmatter:
            raise TransactionRejected(f"unknown citation: {unknown_frontmatter[0]}")

    @staticmethod
    def _path_hash(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    @staticmethod
    def _path_key(path: str) -> str:
        return unicodedata.normalize("NFC", path).casefold()
