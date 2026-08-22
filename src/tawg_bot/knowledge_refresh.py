"""Bounded semantic refresh from immutable evidence into the canonical vault."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import Field, ValidationError

from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.models import SourceCursors, SourceRecord, StrictModel
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault_transaction import (
    TransactionRejected,
    VaultTransaction,
    VaultTransactionEngine,
)

_NON_ENGLISH = re.compile(r"[\u0080-\U0010ffff]")
_CURSOR_PATH = "data/state/source-cursors.json"
_CORE_PATHS = frozenset(
    {
        "knowledge/index.md",
        "knowledge/hot.md",
        "knowledge/meta/source-ledger.json",
        "knowledge/meta/claim-ledger.json",
    }
)


class KnowledgeRefreshRejected(ValueError):
    """Raised when a semantic refresh cannot safely be committed."""


class KnowledgeAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["knowledge"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class KnowledgeResult(StrictModel):
    schema_version: Literal["tawg.knowledge-result.v1"]
    transaction: VaultTransaction
    english_summaries: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    processed_record_ids: tuple[str, ...]
    changed_paths: tuple[str, ...]
    index_rebuilt: bool


class KnowledgeRefresh:
    def __init__(
        self,
        root: Path,
        *,
        ai: KnowledgeAi,
        max_records: int = 100,
        max_context_chars: int = 250_000,
        max_budget_usd: str = "2.00",
        timeout_seconds: float = 300,
    ) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.root = root.resolve()
        self.ai = ai
        self.max_records = max_records
        self.max_context_chars = max_context_chars
        self.max_budget_usd = max_budget_usd
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def run(self, *, cutoff: datetime, operation_id: str) -> RefreshResult:
        self._require_utc(cutoff)
        cursor = self._load_cursor()
        pending = self._pending_records(cursor, cutoff)[: self.max_records]
        if not pending:
            return RefreshResult((), (), False)

        context = self._context(pending, cutoff, operation_id)
        raw_result = await self.ai.run(
            job_type="knowledge",
            context_pack=context,
            operation_id=operation_id,
            max_budget_usd=self.max_budget_usd,
            timeout_seconds=self.timeout_seconds,
        )
        result = self._validate_result(raw_result, pending, operation_id)
        engine = VaultTransactionEngine(self.root)
        try:
            inspection = engine.inspect(result.transaction)
            uow = RepositoryUnitOfWork(self.root, operation_id=operation_id)
            engine.stage(result.transaction, inspection.approval_sha256, uow)
            last = pending[-1]
            updated_cursor = cursor.model_copy(
                update={
                    "knowledge_record_id": last.record_id,
                    "knowledge_updated_at": self._semantic_time(last),
                }
            )
            uow.stage_bytes(
                _CURSOR_PATH,
                (updated_cursor.model_dump_json(indent=2) + "\n").encode("utf-8"),
            )
            published = uow.publish()
        except (TransactionRejected, ValueError) as error:
            raise KnowledgeRefreshRejected(str(error)) from None

        rebuilt = True
        try:
            VaultRetriever(self.root).build()
        except (OSError, UnicodeError, ValueError):
            rebuilt = False
        return RefreshResult(
            tuple(record.record_id for record in pending), published.changed_paths, rebuilt
        )

    def _load_cursor(self) -> SourceCursors:
        path = self.root / _CURSOR_PATH
        if not path.exists():
            return SourceCursors()
        try:
            return SourceCursors.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as error:
            raise KnowledgeRefreshRejected("invalid source cursor state") from error

    def _pending_records(
        self, cursor: SourceCursors, cutoff: datetime
    ) -> list[SourceRecord]:
        lower = (
            (cursor.knowledge_updated_at, cursor.knowledge_record_id or "")
            if cursor.knowledge_updated_at is not None
            else None
        )
        candidates = []
        for record in SourceQuery(self.root).records():
            key = (self._semantic_time(record), record.record_id)
            if key[0] <= cutoff and (lower is None or key > lower):
                candidates.append(record)
        candidates.sort(key=lambda item: (self._semantic_time(item), item.record_id))
        return candidates

    def _context(
        self, records: list[SourceRecord], cutoff: datetime, operation_id: str
    ) -> str:
        record_payloads = [record.model_dump(mode="json") for record in records]
        query_text = " ".join(record.text_original for record in records)[:8000]
        retrieved = [
            {
                "chunk_id": item.chunk_id,
                "path": item.path,
                "text": item.text,
                "record_id": item.record_id,
                "source_locator": item.source_locator,
                "score": item.score,
            }
            for item in VaultRetriever(self.root).query(query_text, top_k=12)
        ]
        aliases = self._yaml_mapping(self.root / "knowledge/meta/aliases.yml")
        schema = self._json_mapping(
            self.root / "src/tawg_bot/schemas/knowledge-result.v1.json"
        )
        inputs = ContextInputs(
            trigger={
                "operation": "refresh_canonical_knowledge",
                "cutoff_utc": cutoff.isoformat(),
                "records": record_payloads,
            },
            reply_chain=[],
            recent_telegram=[
                item for item in record_payloads if item["source_type"] == "telegram_message"
            ],
            retrieved=retrieved,
            citations=[
                {
                    "record_id": record.record_id,
                    "source_locator": record.source_locator,
                }
                for record in records
            ],
            aliases=aliases,
            job_state={"operation_id": operation_id, "cutoff_utc": cutoff.isoformat()},
            allowed_paths=["knowledge/"],
            output_schema=schema,
            budgets={
                "max_transaction_bytes": 1_048_576,
                "max_writes": 64,
                "max_records": len(records),
            },
        )
        try:
            return ContextPackBuilder(self.privacy).build(
                inputs, max_chars=self.max_context_chars, max_recent_telegram=len(records)
            ).text
        except ContextRejected as error:
            raise KnowledgeRefreshRejected(str(error)) from None

    def _validate_result(
        self,
        raw: Mapping[str, Any],
        records: list[SourceRecord],
        operation_id: str,
    ) -> KnowledgeResult:
        try:
            result = KnowledgeResult.model_validate(raw)
        except ValidationError as error:
            raise KnowledgeRefreshRejected("invalid knowledge result") from error
        if result.transaction.operation_id != operation_id:
            raise KnowledgeRefreshRejected("knowledge operation_id mismatch")
        paths = {write.path for write in result.transaction.writes}
        missing_paths = sorted(_CORE_PATHS - paths)
        if missing_paths:
            raise KnowledgeRefreshRejected(
                f"knowledge result omits core path: {missing_paths[0]}"
            )
        selected_ids = {record.record_id for record in records}
        unknown_summaries = set(result.english_summaries) - selected_ids
        if unknown_summaries:
            raise KnowledgeRefreshRejected("English summary references an unknown record")
        for record in records:
            if _NON_ENGLISH.search(record.text_original):
                summary = result.english_summaries.get(record.record_id, "").strip()
                if not summary:
                    raise KnowledgeRefreshRejected(
                        f"English summary is required for {record.record_id}"
                    )
                try:
                    self.privacy.assert_public(summary)
                except PrivacyViolation:
                    raise KnowledgeRefreshRejected(
                        "English summary failed privacy policy"
                    ) from None

        source_write = next(
            write
            for write in result.transaction.writes
            if write.path == "knowledge/meta/source-ledger.json"
        )
        try:
            source_ledger = json.loads(source_write.content)
            entries = source_ledger["entries"]
            if (
                source_ledger.get("schema") != "tawg.source-ledger.v1"
                or not isinstance(entries, dict)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise KnowledgeRefreshRejected("invalid source ledger output") from None
        known_ids = {record.record_id for record in SourceQuery(self.root).records()}
        if not selected_ids.issubset(entries):
            raise KnowledgeRefreshRejected("source ledger omits selected evidence")
        if not set(entries).issubset(known_ids):
            raise KnowledgeRefreshRejected("source ledger contains fabricated evidence")
        return result

    @staticmethod
    def _semantic_time(record: SourceRecord) -> datetime:
        return max(record.updated_at, record.ingested_at)

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("knowledge cutoff must use UTC")

    @staticmethod
    def _json_mapping(path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise KnowledgeRefreshRejected("knowledge schema must be an object")
        return raw

    @staticmethod
    def _yaml_mapping(path: Path) -> dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise KnowledgeRefreshRejected("alias registry must be a mapping")
        return raw
