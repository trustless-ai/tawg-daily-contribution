"""Bounded in-memory collection of current Telegram, GitHub, and forum activity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx

from tawg_bot.github_source import GitHubHttpClient, GitHubSource
from tawg_bot.magicians_source import (
    MagiciansHttpClient,
    MagiciansSource,
    TopicSeed,
)
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.source_registry import EvidenceKind, SourceRegistry


class DailyEvidenceRejected(ValueError):
    """Raised when current Daily activity cannot be collected safely."""


class DailyWindowLike(Protocol):
    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> datetime: ...

    def contains(self, value: datetime) -> bool: ...


class ActivityRecordsClient(Protocol):
    async def collect_records(
        self, *, since: datetime, now: datetime
    ) -> tuple[SourceRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class DailyEvidence:
    evidence_id: str
    source_kind: Literal["telegram", "github", "magicians"]
    source_url: str
    created_at: datetime
    updated_at: datetime
    author_person_id: str | None
    text: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source_url or not self.text:
            raise ValueError("Daily evidence fields cannot be empty")
        _require_utc(self.created_at, "Daily evidence creation time")
        _require_utc(self.updated_at, "Daily evidence update time")
        if self.updated_at < self.created_at:
            raise ValueError("Daily evidence update cannot precede creation")
        if self.source_kind != "telegram":
            parsed = urlparse(self.source_url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
            ):
                raise ValueError("external Daily evidence URL must be stable HTTPS")

    @property
    def citation(self) -> str:
        return self.evidence_id if self.source_kind == "telegram" else self.source_url


class DailyEvidenceCollector:
    def __init__(
        self,
        root: Path,
        *,
        github: ActivityRecordsClient,
        magicians: ActivityRecordsClient,
        max_items: int = 500,
        max_total_chars: int = 250_000,
        timeout_seconds: float = 120,
    ) -> None:
        if max_items <= 0 or max_total_chars <= 0 or timeout_seconds <= 0:
            raise ValueError("Daily evidence budgets must be positive")
        self.root = root.resolve()
        self.github = github
        self.magicians = magicians
        self.max_items = max_items
        self.max_total_chars = max_total_chars
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def collect(self, window: DailyWindowLike, *, now: datetime) -> tuple[DailyEvidence, ...]:
        _require_utc(now, "Daily evidence collection time")
        if now < window.end:
            raise DailyEvidenceRejected("Daily evidence cannot be collected before cutoff")
        try:
            github_records, magicians_records = await asyncio.wait_for(
                asyncio.gather(
                    self.github.collect_records(since=window.start, now=now),
                    self.magicians.collect_records(since=window.start, now=now),
                ),
                timeout=self.timeout_seconds,
            )
        except (TimeoutError, OSError, RuntimeError, ValueError):
            raise DailyEvidenceRejected("live Daily activity collection failed") from None
        records = [
            *SourceQuery(self.root).records(),
            *github_records,
            *magicians_records,
        ]
        selected = [
            record
            for record in records
            if window.contains(record.updated_at) and record.text_original.strip()
        ]
        selected.sort(key=lambda record: (record.updated_at, record.record_id))
        by_id: dict[str, DailyEvidence] = {}
        total_chars = 0
        for record in selected:
            if len(by_id) >= self.max_items:
                break
            evidence = self._convert(record)
            total_chars += len(evidence.text)
            if total_chars > self.max_total_chars:
                raise DailyEvidenceRejected("live Daily activity exceeded its text budget")
            existing = by_id.get(evidence.evidence_id)
            if existing is not None and existing != evidence:
                raise DailyEvidenceRejected("conflicting live Daily evidence")
            by_id[evidence.evidence_id] = evidence
        return tuple(by_id[key] for key in by_id)

    def _convert(self, record: SourceRecord) -> DailyEvidence:
        if record.source_type is SourceType.TELEGRAM_MESSAGE:
            source_kind: Literal["telegram", "github", "magicians"] = "telegram"
        elif record.source_type is SourceType.MAGICIANS_POST:
            source_kind = "magicians"
        elif record.source_type.value.startswith("github_"):
            source_kind = "github"
        else:
            raise DailyEvidenceRejected("unsupported Daily evidence type")
        try:
            self.privacy.assert_public(record.text_original)
        except PrivacyViolation:
            raise DailyEvidenceRejected("Daily evidence failed privacy validation") from None
        try:
            return DailyEvidence(
                evidence_id=record.record_id,
                source_kind=source_kind,
                source_url=record.source_locator,
                created_at=record.created_at,
                updated_at=record.updated_at,
                author_person_id=record.author_person_id,
                text=record.text_original,
            )
        except ValueError:
            raise DailyEvidenceRejected("Daily evidence metadata is invalid") from None


class GitHubActivityRecords:
    def __init__(self, root: Path, *, client: httpx.AsyncClient) -> None:
        self.root = root.resolve()
        self.client = client

    async def collect_records(self, *, since: datetime, now: datetime) -> tuple[SourceRecord, ...]:
        source = GitHubSource.for_repository(
            root=self.root,
            client=GitHubHttpClient.from_env(client=self.client),
            now=lambda: now,
        )
        batch = await source.sync_activity_since(since)
        if not batch.successful:
            raise DailyEvidenceRejected("GitHub Daily activity collection failed")
        return tuple(record for record in batch.records if record.updated_at <= now)


class MagiciansActivityRecords:
    _BASE_URL = "https://ethereum-magicians.org"

    def __init__(
        self,
        root: Path,
        *,
        client: httpx.AsyncClient,
        registry: SourceRegistry,
    ) -> None:
        self.root = root.resolve()
        self.client = client
        self.registry = registry

    async def collect_records(self, *, since: datetime, now: datetime) -> tuple[SourceRecord, ...]:
        seeds = self._seeds()
        source = MagiciansSource(
            client=MagiciansHttpClient(base_url=self._BASE_URL, client=self.client),
            base_url=self._BASE_URL,
            privacy=PrivacyFilter.from_yaml(self.root / "config/privacy.yml"),
            now=lambda: now,
            max_topic_posts=1_000,
        )
        batch = await source.sync_activity_since(seeds, since)
        if not batch.successful:
            raise DailyEvidenceRejected("Magicians Daily activity collection failed")
        return tuple(record for record in batch.records if since <= record.updated_at <= now)

    def _seeds(self) -> tuple[TopicSeed, ...]:
        by_id: dict[int, TopicSeed] = {}
        for erc_number in self.registry.erc_numbers():
            for source in self.registry.resolve(erc_number, frozenset({EvidenceKind.DISCUSSION})):
                parts = [part for part in urlparse(source.canonical_url).path.split("/") if part]
                if len(parts) < 3 or parts[0] != "t":
                    continue
                try:
                    topic_id = int(parts[1] if parts[1].isdigit() else parts[2])
                except ValueError:
                    continue
                slug = "topic" if parts[1].isdigit() else parts[1]
                by_id[topic_id] = TopicSeed(topic_id, slug, "source_registry")
        return tuple(by_id[key] for key in sorted(by_id))


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
