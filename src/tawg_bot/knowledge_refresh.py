"""Compile transient live evidence into bounded, generated canonical knowledge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import Field, ValidationError

from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.erc_query import ErcIntent, ErcQuery
from tawg_bot.knowledge_jobs import KnowledgeRefreshJob, KnowledgeStateStore
from tawg_bot.ledger import (
    ClaimAssessmentV2,
    EvidenceLedger,
    InsufficientEvidence,
    SourceEvidenceV2,
)
from tawg_bot.live_evidence import EvidenceItem, EvidencePack
from tawg_bot.models import StrictModel
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.source_registry import SourceRegistry
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import parse_frontmatter
from tawg_bot.vault_transaction import (
    CitationScope,
    TransactionRejected,
    VaultTransaction,
    VaultTransactionEngine,
)

_SOURCE_LEDGER = "knowledge/meta/source-ledger.json"
_CLAIM_LEDGER = "knowledge/meta/claim-ledger.json"
_MIN_CONTEXT_EVIDENCE_CHARS = 1_000
_PRIORITY_CONTEXT_REJECTION = "priority context does not fit the configured budget"
_REQUIRED_SECTIONS = (
    "Summary",
    "Status",
    "Motivation",
    "Architecture",
    "Interfaces",
    "State machine",
    "Implementation",
    "Security considerations",
    "Testing and examples",
    "Open questions",
)


class KnowledgeRefreshRejected(ValueError):
    """Raised when a live-evidence compilation cannot safely be committed."""


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


class LiveEvidenceProvider(Protocol):
    async def build(self, query: ErcQuery, *, now: datetime) -> EvidencePack: ...


class ResultGap(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    bucket: str = Field(min_length=1, max_length=64)
    source_key: str | None = Field(default=None, max_length=128)
    safe_error_code: str = Field(min_length=1, max_length=64)


class KnowledgeResult(StrictModel):
    schema_version: Literal["tawg.knowledge-result.v2"]
    processed_job_keys: list[str]
    evidence_gaps: list[ResultGap]
    transaction: VaultTransaction


@dataclass(frozen=True, slots=True)
class RefreshResult:
    processed_job_keys: tuple[str, ...]
    changed_paths: tuple[str, ...]
    index_rebuilt: bool


class KnowledgeRefresh:
    def __init__(
        self,
        root: Path,
        *,
        ai: KnowledgeAi,
        live_evidence: LiveEvidenceProvider,
        registry: SourceRegistry,
        max_jobs: int = 32,
        max_ercs_per_run: int = 2,
        max_context_chars: int = 160_000,
        max_budget_usd: str = "2.00",
        timeout_seconds: float = 900,
    ) -> None:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        if max_ercs_per_run <= 0:
            raise ValueError("max_ercs_per_run must be positive")
        self.root = root.resolve()
        self.ai = ai
        self.live_evidence = live_evidence
        self.registry = registry
        self.state = KnowledgeStateStore(self.root, registry=registry)
        self.max_jobs = max_jobs
        self.max_ercs_per_run = max_ercs_per_run
        self.max_context_chars = max_context_chars
        self.max_budget_usd = max_budget_usd
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def run(
        self,
        *,
        cutoff: datetime,
        operation_id: str,
        erc_numbers: frozenset[int] | None = None,
        dry_run: bool = False,
    ) -> RefreshResult:
        self._require_utc(cutoff)
        pending = self._pending_jobs(cutoff, erc_numbers)
        if not pending:
            return RefreshResult((), (), False)
        selected_erc_numbers = tuple(dict.fromkeys(job.erc_number for job in pending))
        packs = tuple(
            [
                await self.live_evidence.build(
                    ErcQuery(erc_numbers=(erc_number,), intent=ErcIntent.IMPLEMENTATION),
                    now=cutoff,
                )
                for erc_number in selected_erc_numbers
            ]
        )
        context = self._context(pending, packs, cutoff, operation_id)
        raw_result = await self.ai.run(
            job_type="knowledge",
            context_pack=context,
            operation_id=operation_id,
            max_budget_usd=self.max_budget_usd,
            timeout_seconds=self.timeout_seconds,
        )
        result = self._validate_result(raw_result, pending, packs, operation_id)
        source_keys = frozenset(item.source_key for pack in packs for item in pack.evidence)
        citation_urls = frozenset(url for pack in packs for url in pack.citation_allowlist)
        engine = VaultTransactionEngine(
            self.root,
            citation_scope=CitationScope(source_keys=source_keys, urls=citation_urls),
        )
        try:
            inspection = engine.inspect(result.transaction)
            if dry_run:
                return RefreshResult(
                    tuple(result.processed_job_keys), inspection.changed_paths, False
                )
            uow = RepositoryUnitOfWork(self.root, operation_id=operation_id)
            uow.register_external_evidence(item.text for pack in packs for item in pack.evidence)
            engine.stage(result.transaction, inspection.approval_sha256, uow)
            processed = frozenset(result.processed_job_keys)
            self.state.stage_compilation_outcome(
                uow,
                tuple(pack.for_persistence() for pack in packs),
                processed,
                now=cutoff,
            )
            published = uow.publish()
        except (TransactionRejected, ValueError) as error:
            raise KnowledgeRefreshRejected(str(error)) from None

        rebuilt = True
        try:
            VaultRetriever(self.root).build()
        except (OSError, UnicodeError, ValueError):
            rebuilt = False
        return RefreshResult(tuple(result.processed_job_keys), published.changed_paths, rebuilt)

    def _pending_jobs(
        self, cutoff: datetime, erc_numbers: frozenset[int] | None
    ) -> list[KnowledgeRefreshJob]:
        jobs = [
            job
            for job in self.state.load().refresh_jobs
            if job.updated_at <= cutoff and (erc_numbers is None or job.erc_number in erc_numbers)
        ]
        jobs.sort(key=lambda job: (job.updated_at, job.job_key))
        groups: dict[int, list[KnowledgeRefreshJob]] = {}
        for job in jobs:
            groups.setdefault(job.erc_number, []).append(job)
        selected: list[KnowledgeRefreshJob] = []
        for group in groups.values():
            if len(group) > self.max_jobs:
                raise KnowledgeRefreshRejected("ERC refresh group exceeds max_jobs")
            if len(selected) + len(group) > self.max_jobs:
                break
            selected.extend(group)
            if len({job.erc_number for job in selected}) == self.max_ercs_per_run:
                break
        return selected

    def _context(
        self,
        jobs: list[KnowledgeRefreshJob],
        packs: tuple[EvidencePack, ...],
        cutoff: datetime,
        operation_id: str,
    ) -> str:
        source_keys = sorted({item.source_key for pack in packs for item in pack.evidence})
        citation_urls = list(
            dict.fromkeys(url for pack in packs for url in pack.citation_allowlist)
        )
        context_packs = [self._context_evidence_pack(pack) for pack in packs]
        inputs = ContextInputs(
            trigger={
                "operation": "compile_live_evidence",
                "cutoff_utc": cutoff.isoformat(),
                "required_erc_sections": list(_REQUIRED_SECTIONS),
                "acknowledgement_page_contract": {
                    "path": "knowledge/acknowledgements/<public-name>.md",
                    "heading": "Related topics",
                    "purpose": "acknowledge public contributions",
                },
            },
            reply_chain=[],
            recent_telegram=[],
            retrieved=[
                orientation.model_dump(mode="json")
                for pack in packs
                for orientation in pack.generated_orientation
            ],
            citations=[
                {"source_key": item.source_key, "url": item.citation_url}
                for pack in packs
                for item in pack.evidence
            ],
            aliases=self._yaml_mapping(self.root / "knowledge/meta/aliases.yml"),
            job_state={
                "operation_id": operation_id,
                "jobs": [job.model_dump(mode="json") for job in jobs],
            },
            allowed_paths=["knowledge/"],
            output_schema=self._json_mapping(
                self.root / "src/tawg_bot/schemas/knowledge-result.v2.json"
            ),
            budgets={
                "max_transaction_bytes": 1_048_576,
                "max_writes": 64,
                "max_jobs": len(jobs),
            },
            evidence_pack={
                "schema_version": "tawg.evidence-batch.v1",
                "packs": context_packs,
                "allowed_source_keys": source_keys,
            },
            citation_allowlist=citation_urls,
        )
        try:
            return self._build_context(inputs)
        except ContextRejected as error:
            if str(error) == _PRIORITY_CONTEXT_REJECTION:
                return self._build_bounded_evidence_context(inputs, context_packs)
            raise KnowledgeRefreshRejected(str(error)) from None

    def _build_context(self, inputs: ContextInputs) -> str:
        return (
            ContextPackBuilder(self.privacy)
            .build(inputs, max_chars=self.max_context_chars, max_recent_telegram=0)
            .text
        )

    def _build_bounded_evidence_context(
        self,
        inputs: ContextInputs,
        context_packs: list[dict[str, Any]],
    ) -> str:
        text_lengths = [len(item["text"]) for pack in context_packs for item in pack["evidence"]]
        if not text_lengths:
            raise KnowledgeRefreshRejected(_PRIORITY_CONTEXT_REJECTION)

        low = min(_MIN_CONTEXT_EVIDENCE_CHARS, max(text_lengths))
        high = max(text_lengths) - 1
        best: str | None = None
        while low <= high:
            text_cap = (low + high) // 2
            candidate = deepcopy(inputs)
            candidate.evidence_pack = self._capped_evidence_pack(context_packs, text_cap=text_cap)
            try:
                best = self._build_context(candidate)
            except ContextRejected as error:
                if str(error) != _PRIORITY_CONTEXT_REJECTION:
                    raise KnowledgeRefreshRejected(str(error)) from None
                high = text_cap - 1
            else:
                low = text_cap + 1
        if best is None:
            raise KnowledgeRefreshRejected(_PRIORITY_CONTEXT_REJECTION)
        return best

    @staticmethod
    def _capped_evidence_pack(
        context_packs: list[dict[str, Any]], *, text_cap: int
    ) -> dict[str, Any]:
        packs = deepcopy(context_packs)
        for pack in packs:
            for item in pack["evidence"]:
                text = item["text"]
                if len(text) > text_cap:
                    item["text"] = text[:text_cap]
                    item["excerpted"] = True
        return {
            "schema_version": "tawg.evidence-batch.v1",
            "packs": packs,
            "allowed_source_keys": sorted(
                {item["source_key"] for pack in packs for item in pack["evidence"]}
            ),
        }

    def _context_evidence_pack(self, pack: EvidencePack) -> dict[str, Any]:
        evidence: list[EvidenceItem] = []
        for item in pack.evidence:
            inspected = self.privacy.inspect(item.text)
            if not inspected.accepted or inspected.sanitized_text is None:
                raise KnowledgeRefreshRejected(
                    f"context privacy rejection: {inspected.reason_code or 'unsafe_text'}"
                )
            evidence.append(item.model_copy(update={"text": inspected.sanitized_text}))
        return pack.model_copy(update={"evidence": evidence}).model_dump(mode="json")

    def _validate_result(
        self,
        raw: Mapping[str, Any],
        jobs: list[KnowledgeRefreshJob],
        packs: tuple[EvidencePack, ...],
        operation_id: str,
    ) -> KnowledgeResult:
        try:
            result = KnowledgeResult.model_validate(raw)
        except ValidationError as error:
            raise KnowledgeRefreshRejected("invalid knowledge result") from error
        expected_jobs = [job.job_key for job in jobs]
        if result.processed_job_keys != expected_jobs:
            raise KnowledgeRefreshRejected("knowledge result job set mismatch")
        if result.transaction.operation_id != operation_id:
            raise KnowledgeRefreshRejected("knowledge operation_id mismatch")
        expected_gaps = sorted(
            (
                missing.erc_number,
                missing.bucket,
                missing.source_key,
                missing.safe_error_code,
            )
            for pack in packs
            for missing in pack.missing_required
        )
        actual_gaps = sorted(
            (gap.erc_number, gap.bucket, gap.source_key, gap.safe_error_code)
            for gap in result.evidence_gaps
        )
        if actual_gaps != expected_gaps:
            raise KnowledgeRefreshRejected("knowledge result evidence gaps mismatch")
        writes = {write.path: write for write in result.transaction.writes}
        if len(writes) != len(result.transaction.writes):
            raise KnowledgeRefreshRejected("knowledge result contains duplicate paths")
        required_paths = {_SOURCE_LEDGER, _CLAIM_LEDGER} | {
            f"knowledge/ercs/erc-{erc}.md" for pack in packs for erc in pack.query.erc_numbers
        }
        missing_paths = sorted(required_paths - set(writes))
        if missing_paths:
            raise KnowledgeRefreshRejected(
                f"knowledge result omits required path: {missing_paths[0]}"
            )
        evidence = {item.source_key: item for pack in packs for item in pack.evidence}
        allowed_urls = {url for pack in packs for url in pack.citation_allowlist}
        self._validate_pages(writes, packs, evidence, allowed_urls)
        source_ledger = self._validate_source_ledger(writes[_SOURCE_LEDGER].content, evidence)
        self._validate_claim_ledger(writes[_CLAIM_LEDGER].content, source_ledger)
        self._reject_copied_bodies(
            (writes[_SOURCE_LEDGER].content, writes[_CLAIM_LEDGER].content), evidence
        )
        return result

    def _validate_pages(
        self,
        writes: Mapping[str, Any],
        packs: tuple[EvidencePack, ...],
        evidence: Mapping[str, EvidenceItem],
        allowed_urls: set[str],
    ) -> None:
        for pack in packs:
            for erc_number in pack.query.erc_numbers:
                path = f"knowledge/ercs/erc-{erc_number}.md"
                write = writes[path]
                frontmatter, body = parse_frontmatter(write.content)
                source_keys = frontmatter.get("source_keys") if frontmatter else None
                telegram_ids = frontmatter.get("telegram_record_ids") if frontmatter else None
                verified_at = frontmatter.get("verified_at") if frontmatter else None
                if (
                    not isinstance(source_keys, list)
                    or not source_keys
                    or not all(isinstance(value, str) for value in source_keys)
                    or not isinstance(telegram_ids, list)
                    or not isinstance(verified_at, str | datetime)
                ):
                    raise KnowledgeRefreshRejected("generated ERC page omits v2 evidence")
                if not set(source_keys).issubset(evidence):
                    raise KnowledgeRefreshRejected("generated ERC page uses an unknown source key")
                for heading in _REQUIRED_SECTIONS:
                    if not re.search(rf"(?m)^## {re.escape(heading)}\s*$", body):
                        raise KnowledgeRefreshRejected(
                            f"generated ERC page omits section: {heading}"
                        )
                urls = {
                    value.rstrip('.,;:!?)"]}')
                    for value in re.findall(r"https://[^\s<>()\]]+", write.content)
                }
                if not urls.issubset(allowed_urls):
                    raise KnowledgeRefreshRejected("generated ERC page uses an unapproved URL")

    def _validate_source_ledger(
        self, content: str, evidence: Mapping[str, EvidenceItem]
    ) -> EvidenceLedger:
        try:
            raw = json.loads(content)
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != "tawg.source-ledger.v2"
                or not isinstance(raw.get("entries"), dict)
            ):
                raise ValueError
            entries = raw["entries"]
            ledger = EvidenceLedger.from_entries(entries, schema="tawg.source-ledger.v2")
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raise KnowledgeRefreshRejected("invalid v2 source ledger output") from None
        existing = self._existing_entries(_SOURCE_LEDGER, "tawg.source-ledger.v2")
        for source_key, entry in entries.items():
            item = evidence.get(source_key)
            if item is None:
                if existing.get(source_key) != entry:
                    raise KnowledgeRefreshRejected(
                        "source ledger contains evidence outside the operation pack"
                    )
                continue
            parsed = ledger.sources[source_key]
            if not isinstance(parsed, SourceEvidenceV2) or (
                parsed.source_kind is not item.kind
                or parsed.authority is not item.authority
                or parsed.canonical_url != item.canonical_url
                or parsed.observed_version != item.version
                or parsed.observed_sha256 != item.content_sha256
                or parsed.observed_at != item.observed_at
            ):
                raise KnowledgeRefreshRejected("source ledger metadata mismatches live evidence")
        if not set(evidence).issubset(entries):
            raise KnowledgeRefreshRejected("source ledger omits operation evidence")
        return ledger

    @staticmethod
    def _validate_claim_ledger(content: str, evidence: EvidenceLedger) -> None:
        try:
            raw = json.loads(content)
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != "tawg.claim-ledger.v2"
                or not isinstance(raw.get("entries"), dict)
            ):
                raise ValueError
            for claim_id, value in raw["entries"].items():
                if not isinstance(value, dict):
                    raise ValueError
                claim = ClaimAssessmentV2.model_validate({"claim_id": claim_id, **value})
                evidence.validate_claim(claim)
        except InsufficientEvidence as error:
            raise KnowledgeRefreshRejected(str(error)) from None
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raise KnowledgeRefreshRejected("invalid v2 claim ledger output") from None

    @staticmethod
    def _reject_copied_bodies(
        ledger_contents: tuple[str, str], evidence: Mapping[str, EvidenceItem]
    ) -> None:
        combined = "\n".join(ledger_contents)
        for item in evidence.values():
            chunks = [line.strip() for line in item.text.splitlines() if len(line.strip()) >= 64]
            if any(chunk in combined for chunk in chunks):
                raise KnowledgeRefreshRejected("external evidence body cannot be stored in ledgers")

    def _existing_entries(self, relative: str, schema: str) -> dict[str, Any]:
        try:
            raw = json.loads((self.root / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != schema:
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

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
