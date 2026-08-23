"""Bounded current state for evidence gaps, refresh work, and source candidates."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, ValidationError, field_validator

from tawg_bot.erc_query import ErcIntent
from tawg_bot.live_evidence import EvidencePersistenceOutcome
from tawg_bot.models import StrictModel
from tawg_bot.source_registry import SourceObservation, SourceRegistry
from tawg_bot.unit_of_work import RepositoryUnitOfWork

_GAPS_PATH = "data/state/knowledge-gaps.json"
_REFRESH_PATH = "data/state/pending-knowledge-refresh.json"
_CANDIDATES_PATH = "data/state/source-candidates.json"
_REGISTRY_PATH = "knowledge/meta/sources.yml"
_SAFE_HOST = re.compile(r"^[a-z0-9.-]+$")


class KnowledgeStateRejected(ValueError):
    """Raised when bounded knowledge state is invalid or unsafe."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("knowledge state timestamp must use UTC")
    return value.astimezone(UTC)


class KnowledgeGap(StrictModel):
    schema_version: str = "tawg.knowledge-gap.v1"
    gap_key: str = Field(min_length=1, max_length=512)
    erc_number: int = Field(ge=1, le=99_999)
    intent: ErcIntent
    bucket: str = Field(min_length=1, max_length=64)
    source_key: str | None = Field(default=None, max_length=128)
    safe_error_code: str = Field(min_length=1, max_length=64)
    first_observed_at: datetime
    last_observed_at: datetime

    _first_utc = field_validator("first_observed_at")(_utc)
    _last_utc = field_validator("last_observed_at")(_utc)


class KnowledgeRefreshJob(StrictModel):
    schema_version: str = "tawg.knowledge-refresh-job.v1"
    job_key: str = Field(min_length=1, max_length=512)
    erc_number: int = Field(ge=1, le=99_999)
    source_key: str = Field(min_length=1, max_length=128)
    observed_version: str | None = Field(default=None, max_length=256)
    observed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime
    retry_count: int = Field(default=0, ge=0, le=100)
    safe_error_code: str | None = Field(default=None, max_length=64)

    _created_utc = field_validator("created_at")(_utc)
    _updated_utc = field_validator("updated_at")(_utc)


class SourceCandidate(StrictModel):
    schema_version: str = "tawg.source-candidate.v1"
    candidate_key: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    trigger_record_id: str = Field(min_length=1, max_length=256)
    suggested_at: datetime
    last_suggested_at: datetime

    _suggested_utc = field_validator("suggested_at")(_utc)
    _last_suggested_utc = field_validator("last_suggested_at")(_utc)


@dataclass(frozen=True, slots=True)
class KnowledgeState:
    gaps: tuple[KnowledgeGap, ...]
    refresh_jobs: tuple[KnowledgeRefreshJob, ...]
    candidates: tuple[SourceCandidate, ...]


def gap_key(
    erc: int,
    intent: ErcIntent,
    bucket: str,
    source_key: str | None,
) -> str:
    source = source_key or "unregistered"
    return f"gap:erc-{erc}:{intent.value}:{bucket}:{source}"


def refresh_key(erc: int, source_key: str, observed_sha256: str) -> str:
    return f"refresh:erc-{erc}:{source_key}:{observed_sha256[:16]}"


class KnowledgeStateStore:
    def __init__(self, root: Path, *, registry: SourceRegistry) -> None:
        self.root = root.resolve()
        self.registry = registry

    def load(self) -> KnowledgeState:
        try:
            gaps = tuple(KnowledgeGap.model_validate(item) for item in self._list(_GAPS_PATH))
            refresh_jobs = tuple(
                KnowledgeRefreshJob.model_validate(item) for item in self._list(_REFRESH_PATH)
            )
            candidates = tuple(
                SourceCandidate.model_validate(item) for item in self._list(_CANDIDATES_PATH)
            )
        except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise KnowledgeStateRejected("invalid knowledge state") from error
        self._unique(gaps, "gap_key")
        self._unique(refresh_jobs, "job_key")
        self._unique(candidates, "candidate_key")
        return KnowledgeState(gaps, refresh_jobs, candidates)

    def stage_evidence_outcome(
        self,
        uow: RepositoryUnitOfWork,
        outcome: EvidencePersistenceOutcome,
        *,
        now: datetime,
    ) -> None:
        _utc(now)
        state = self.load()
        gaps = {gap.gap_key: gap for gap in state.gaps}
        current_keys = {
            gap_key(
                missing.erc_number,
                missing.intent,
                missing.bucket,
                missing.source_key,
            )
            for missing in outcome.missing_required
        }
        for key, gap in list(gaps.items()):
            if (
                gap.erc_number in outcome.query.erc_numbers
                and gap.intent is outcome.query.intent
                and key not in current_keys
            ):
                del gaps[key]
        for missing in outcome.missing_required:
            key = gap_key(
                missing.erc_number,
                missing.intent,
                missing.bucket,
                missing.source_key,
            )
            existing_gap = gaps.get(key)
            gaps[key] = KnowledgeGap(
                gap_key=key,
                erc_number=missing.erc_number,
                intent=missing.intent,
                bucket=missing.bucket,
                source_key=missing.source_key,
                safe_error_code=missing.safe_error_code,
                first_observed_at=(existing_gap.first_observed_at if existing_gap else now),
                last_observed_at=now,
            )

        jobs = {job.job_key: job for job in state.refresh_jobs}
        for change in outcome.source_changes:
            key = refresh_key(
                change.erc_number,
                change.source_key,
                change.observed_sha256,
            )
            existing_job = jobs.get(key)
            jobs = {
                existing_key: job
                for existing_key, job in jobs.items()
                if not (job.erc_number == change.erc_number and job.source_key == change.source_key)
            }
            jobs[key] = KnowledgeRefreshJob(
                job_key=key,
                erc_number=change.erc_number,
                source_key=change.source_key,
                observed_version=change.observed_version,
                observed_sha256=change.observed_sha256,
                created_at=existing_job.created_at if existing_job else now,
                updated_at=now,
                retry_count=existing_job.retry_count if existing_job else 0,
                safe_error_code=(existing_job.safe_error_code if existing_job else None),
            )

        observations: dict[str, SourceObservation] = {}
        changed_keys = {change.source_key for change in outcome.source_changes}
        for item in outcome.observations:
            if item.source_key not in changed_keys:
                continue
            observations[item.source_key] = SourceObservation(
                checked_at=item.observed_at,
                version=item.version,
                content_sha256=item.content_sha256,
                byte_count=item.source_byte_count,
            )
        if observations:
            uow.stage_bytes(
                _REGISTRY_PATH,
                self.registry.render_with_observations(observations).encode("utf-8"),
            )
        self._stage_state(
            uow,
            gaps=tuple(gaps.values()),
            refresh_jobs=tuple(jobs.values()),
            candidates=state.candidates,
        )

    def resolve_refresh(self, uow: RepositoryUnitOfWork, job_key: str) -> None:
        state = self.load()
        jobs = tuple(job for job in state.refresh_jobs if job.job_key != job_key)
        self._stage_state(
            uow,
            gaps=state.gaps,
            refresh_jobs=jobs,
            candidates=state.candidates,
        )

    def stage_compilation_outcome(
        self,
        uow: RepositoryUnitOfWork,
        outcomes: tuple[EvidencePersistenceOutcome, ...],
        processed_job_keys: frozenset[str],
        *,
        now: datetime,
    ) -> None:
        _utc(now)
        state = self.load()
        gaps = {gap.gap_key: gap for gap in state.gaps}
        jobs = {job.job_key: job for job in state.refresh_jobs}
        observations: dict[str, SourceObservation] = {}
        for outcome in outcomes:
            current_gap_keys = {
                gap_key(
                    missing.erc_number,
                    missing.intent,
                    missing.bucket,
                    missing.source_key,
                )
                for missing in outcome.missing_required
            }
            for key, gap in list(gaps.items()):
                if (
                    gap.erc_number in outcome.query.erc_numbers
                    and gap.intent is outcome.query.intent
                    and key not in current_gap_keys
                ):
                    del gaps[key]
            for missing in outcome.missing_required:
                key = gap_key(
                    missing.erc_number,
                    missing.intent,
                    missing.bucket,
                    missing.source_key,
                )
                existing_gap = gaps.get(key)
                gaps[key] = KnowledgeGap(
                    gap_key=key,
                    erc_number=missing.erc_number,
                    intent=missing.intent,
                    bucket=missing.bucket,
                    source_key=missing.source_key,
                    safe_error_code=missing.safe_error_code,
                    first_observed_at=(existing_gap.first_observed_at if existing_gap else now),
                    last_observed_at=now,
                )
            for change in outcome.source_changes:
                key = refresh_key(
                    change.erc_number,
                    change.source_key,
                    change.observed_sha256,
                )
                existing_job = jobs.get(key)
                jobs = {
                    existing_key: job
                    for existing_key, job in jobs.items()
                    if not (
                        job.erc_number == change.erc_number and job.source_key == change.source_key
                    )
                }
                jobs[key] = KnowledgeRefreshJob(
                    job_key=key,
                    erc_number=change.erc_number,
                    source_key=change.source_key,
                    observed_version=change.observed_version,
                    observed_sha256=change.observed_sha256,
                    created_at=existing_job.created_at if existing_job else now,
                    updated_at=now,
                    retry_count=existing_job.retry_count if existing_job else 0,
                    safe_error_code=(existing_job.safe_error_code if existing_job else None),
                )
            for item in outcome.observations:
                observations[item.source_key] = SourceObservation(
                    checked_at=item.observed_at,
                    version=item.version,
                    content_sha256=item.content_sha256,
                    byte_count=item.source_byte_count,
                )
        for key in processed_job_keys:
            jobs.pop(key, None)
        if observations:
            uow.stage_bytes(
                _REGISTRY_PATH,
                self.registry.render_with_observations(observations).encode("utf-8"),
            )
        self._stage_state(
            uow,
            gaps=tuple(gaps.values()),
            refresh_jobs=tuple(jobs.values()),
            candidates=state.candidates,
        )

    def add_candidate(
        self,
        uow: RepositoryUnitOfWork,
        url: str,
        trigger_record_id: str,
        now: datetime,
    ) -> None:
        self.add_candidates(uow, (url,), trigger_record_id, now)

    def add_candidates(
        self,
        uow: RepositoryUnitOfWork,
        urls: tuple[str, ...],
        trigger_record_id: str,
        now: datetime,
    ) -> None:
        _utc(now)
        if not 1 <= len(urls) <= 4:
            raise KnowledgeStateRejected("candidate_count")
        state = self.load()
        candidates = {candidate.candidate_key: candidate for candidate in state.candidates}
        for url in urls:
            normalized = _candidate_url(url)
            key = f"candidate:{hashlib.sha256(normalized.encode()).hexdigest()[:32]}"
            existing = candidates.get(key)
            candidates[key] = SourceCandidate(
                candidate_key=key,
                url=normalized,
                trigger_record_id=trigger_record_id,
                suggested_at=existing.suggested_at if existing else now,
                last_suggested_at=now,
            )
        self._stage_state(
            uow,
            gaps=state.gaps,
            refresh_jobs=state.refresh_jobs,
            candidates=tuple(candidates.values()),
        )

    def _list(self, relative_path: str) -> list[object]:
        path = self.root / relative_path
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("knowledge state must be a list")
        return raw

    @staticmethod
    def _unique(values: tuple[StrictModel, ...], key_name: str) -> None:
        keys = [getattr(value, key_name) for value in values]
        if len(keys) != len(set(keys)):
            raise KnowledgeStateRejected("invalid knowledge state")

    @staticmethod
    def _stage_state(
        uow: RepositoryUnitOfWork,
        *,
        gaps: tuple[KnowledgeGap, ...],
        refresh_jobs: tuple[KnowledgeRefreshJob, ...],
        candidates: tuple[SourceCandidate, ...],
    ) -> None:
        uow.stage_json(
            _GAPS_PATH,
            [
                item.model_dump(mode="json")
                for item in sorted(gaps, key=lambda value: value.gap_key)
            ],
        )
        uow.stage_json(
            _REFRESH_PATH,
            [
                item.model_dump(mode="json")
                for item in sorted(refresh_jobs, key=lambda value: value.job_key)
            ],
        )
        uow.stage_json(
            _CANDIDATES_PATH,
            [
                item.model_dump(mode="json")
                for item in sorted(candidates, key=lambda value: value.candidate_key)
            ],
        )


def _candidate_url(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise KnowledgeStateRejected("candidate_url")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
    ):
        raise KnowledgeStateRejected("candidate_url")
    host = parsed.hostname.casefold().rstrip(".")
    if not _SAFE_HOST.fullmatch(host):
        raise KnowledgeStateRejected("candidate_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise KnowledgeStateRejected("candidate_url")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))
