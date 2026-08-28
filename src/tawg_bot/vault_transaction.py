"""Inspect-then-apply transactions confined to canonical knowledge files."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from tawg_bot.models import StrictModel
from tawg_bot.persistence_guard import PersistenceProvenance
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


@dataclass(frozen=True, slots=True)
class CitationScope:
    source_keys: frozenset[str]
    urls: frozenset[str]


class VaultTransactionEngine:
    _ALLOWED_SUFFIXES = frozenset({".md", ".json", ".yml", ".yaml"})
    _MAX_TOTAL_BYTES = 1024 * 1024

    def __init__(self, root: Path, *, citation_scope: CitationScope | None = None) -> None:
        self.root = root.resolve()
        self.citation_scope = citation_scope
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
                self._validate_citations(write, known_sources, target)
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
        uow.register_external_evidence(())
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
            uow.stage_bytes(
                write.path,
                write.content.encode("utf-8"),
                provenance=(
                    PersistenceProvenance.SOURCE_METADATA
                    if write.path.startswith("knowledge/meta/")
                    else PersistenceProvenance.GENERATED_KNOWLEDGE
                ),
            )

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
                or relative.parts[1].casefold() == "people"
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

    def _validate_citations(
        self,
        write: VaultWrite,
        known_sources: Mapping[str, object],
        target: Path,
    ) -> None:
        if self.citation_scope is not None:
            self._validate_scoped_citations(write, known_sources)
        else:
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
        self._validate_provenance_expansion(write, target)

    @staticmethod
    def _validate_provenance_expansion(write: VaultWrite, target: Path) -> None:
        frontmatter, _ = parse_frontmatter(write.content)
        if frontmatter is None:
            return
        current_frontmatter: dict[str, object] = {}
        if target.exists():
            try:
                parsed, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
            except UnicodeError:
                raise TransactionRejected(
                    f"existing Markdown is not valid UTF-8: {write.path}"
                ) from None
            current_frontmatter = parsed or {}
        for key in ("source_ids", "telegram_record_ids"):
            proposed = frontmatter.get(key, [])
            current = current_frontmatter.get(key, [])
            if not isinstance(proposed, list) or not all(
                isinstance(value, str) for value in proposed
            ):
                continue
            current_values = (
                {value for value in current if isinstance(value, str)}
                if isinstance(current, list)
                else set()
            )
            added = set(proposed) - current_values
            if not added.issubset(write.citations):
                raise TransactionRejected(
                    f"undeclared provenance in Markdown frontmatter: {write.path}"
                )

    def _validate_scoped_citations(
        self, write: VaultWrite, known_sources: Mapping[str, object]
    ) -> None:
        assert self.citation_scope is not None
        if not write.citations:
            raise TransactionRejected(f"Markdown write has no citations: {write.path}")
        source_keys = set(self.citation_scope.source_keys)
        allowed_urls = set(self.citation_scope.urls)
        citations = set(write.citations)
        unknown = citations - source_keys - allowed_urls - set(known_sources)
        if unknown:
            raise TransactionRejected(f"unknown citation: {sorted(unknown)[0]}")
        frontmatter, _ = parse_frontmatter(write.content)
        if frontmatter is None:
            raise TransactionRejected(f"Markdown frontmatter omits citations: {write.path}")
        if "source_urls" in frontmatter:
            self._validate_general_scoped_citations(
                write,
                frontmatter,
                citations=citations,
                allowed_urls=allowed_urls,
                known_sources=known_sources,
            )
            return
        page_source_keys = frontmatter.get("source_keys")
        telegram_ids = frontmatter.get("telegram_record_ids")
        verified_at = frontmatter.get("verified_at")
        if (
            not isinstance(page_source_keys, list)
            or not page_source_keys
            or not all(isinstance(value, str) for value in page_source_keys)
            or not isinstance(telegram_ids, list)
            or not all(isinstance(value, str) for value in telegram_ids)
            or not isinstance(verified_at, str | datetime)
        ):
            raise TransactionRejected(f"Markdown frontmatter omits v2 evidence: {write.path}")
        if not set(page_source_keys).issubset(source_keys):
            raise TransactionRejected(f"unknown source key: {write.path}")
        if not set(telegram_ids).issubset(known_sources):
            raise TransactionRejected(f"unknown Telegram citation: {write.path}")
        if not (citations & source_keys).issubset(page_source_keys):
            raise TransactionRejected(f"Markdown frontmatter omits source keys: {write.path}")
        if not (citations & set(known_sources)).issubset(telegram_ids):
            raise TransactionRejected(f"Markdown frontmatter omits Telegram IDs: {write.path}")
        urls = set(re.findall(r"https://[^\s<>()\]]+", write.content))
        normalized_urls = {value.rstrip('.,;:!?)"]}') for value in urls}
        if not normalized_urls.issubset(allowed_urls):
            raise TransactionRejected(f"unapproved source link: {write.path}")
        if not (citations & allowed_urls).issubset(normalized_urls):
            raise TransactionRejected(f"cited source link is absent from page: {write.path}")

    @staticmethod
    def _validate_general_scoped_citations(
        write: VaultWrite,
        frontmatter: Mapping[str, object],
        *,
        citations: set[str],
        allowed_urls: set[str],
        known_sources: Mapping[str, object],
    ) -> None:
        source_urls = frontmatter.get("source_urls")
        source_ids = frontmatter.get("source_ids")
        if (
            not isinstance(source_urls, list)
            or not source_urls
            or not all(isinstance(value, str) for value in source_urls)
            or not isinstance(source_ids, list)
            or not all(isinstance(value, str) for value in source_ids)
        ):
            raise TransactionRejected(
                f"Markdown frontmatter omits general evidence: {write.path}"
            )
        page_urls = set(source_urls)
        page_source_ids = set(source_ids)
        if not page_urls.issubset(allowed_urls):
            raise TransactionRejected(f"unapproved source link: {write.path}")
        if not page_urls.issubset(citations):
            raise TransactionRejected(f"undeclared source link: {write.path}")
        if not page_source_ids.issubset(known_sources):
            raise TransactionRejected(f"unknown citation: {write.path}")
        if not (citations & allowed_urls).issubset(page_urls):
            raise TransactionRejected(
                f"Markdown frontmatter omits source URLs: {write.path}"
            )
        if not (citations & set(known_sources)).issubset(page_source_ids):
            raise TransactionRejected(
                f"Markdown frontmatter omits source IDs: {write.path}"
            )
        urls = set(re.findall(r"https://[^\s<>()\]]+", write.content))
        normalized_urls = {value.rstrip('.,;:!?)"]}') for value in urls}
        if not page_urls.issubset(normalized_urls):
            raise TransactionRejected(f"source link is absent from page: {write.path}")

    @staticmethod
    def _path_hash(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    @staticmethod
    def _path_key(path: str) -> str:
        return unicodedata.normalize("NFC", path).casefold()
