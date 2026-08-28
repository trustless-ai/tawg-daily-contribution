"""Bounded in-memory collection of current Telegram, GitHub, and forum activity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx

from tawg_bot.github_source import GitHubClient, GitHubHttpClient, GitHubSource
from tawg_bot.ids import github_id
from tawg_bot.magicians_source import (
    MagiciansHttpClient,
    MagiciansSource,
    TopicSeed,
)
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.scan_targets import (
    ScanTargetRegistry,
    magicians_topic_id,
    proposal_pr_number,
)


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
                    self._collect_source(
                        "github", self.github, since=window.start, now=now
                    ),
                    self._collect_source(
                        "magicians", self.magicians, since=window.start, now=now
                    ),
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            _log_source_failure("all", "daily_collection_timeout")
            github_records, magicians_records = (), ()
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

    @staticmethod
    async def _collect_source(
        name: Literal["github", "magicians"],
        client: ActivityRecordsClient,
        *,
        since: datetime,
        now: datetime,
    ) -> tuple[SourceRecord, ...]:
        try:
            return await client.collect_records(since=since, now=now)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log_source_failure(name, "daily_source_failed")
            return ()

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
    _MAX_PROPOSAL_PAGES = 3

    def __init__(
        self,
        root: Path,
        *,
        client: httpx.AsyncClient,
        scan_registry: ScanTargetRegistry | None = None,
    ) -> None:
        self.root = root.resolve()
        self.client = client
        self.scan_registry = scan_registry

    async def collect_records(self, *, since: datetime, now: datetime) -> tuple[SourceRecord, ...]:
        github = GitHubHttpClient.from_env(client=self.client)
        source = GitHubSource.for_repository(
            root=self.root,
            client=github,
            now=lambda: now,
        )
        batch = await source.sync_activity_since(since)
        if not batch.successful:
            raise DailyEvidenceRejected("GitHub Daily activity collection failed")
        proposal_records = (
            await self._proposal_records(github, since=since, now=now)
            if self.scan_registry is not None
            else ()
        )
        by_id = {
            record.record_id: record
            for record in (*batch.records, *proposal_records)
            if record.updated_at <= now
        }
        return tuple(by_id[key] for key in sorted(by_id))

    async def _proposal_records(
        self,
        client: GitHubClient,
        *,
        since: datetime,
        now: datetime,
    ) -> tuple[SourceRecord, ...]:
        assert self.scan_registry is not None
        records: list[SourceRecord] = []
        for target in self.scan_registry.ercs:
            if target.proposal_pr_url is None:
                continue
            number = proposal_pr_number(target.proposal_pr_url)
            pull = await client.get_json(f"/repos/ethereum/ERCs/pulls/{number}")
            if not isinstance(pull, dict) or pull.get("number") != number:
                raise DailyEvidenceRejected("proposal PR Daily metadata is invalid")
            record = self._github_record(
                pull,
                kind="pr",
                source_type=SourceType.GITHUB_PULL_REQUEST,
                fallback_url=target.proposal_pr_url,
                fallback_created=now,
            )
            if record is not None and since <= record.updated_at <= now:
                records.append(record)
            for kind, source_type, path in (
                (
                    "issue-comment",
                    SourceType.GITHUB_COMMENT,
                    f"/repos/ethereum/ERCs/issues/{number}/comments",
                ),
                (
                    "review",
                    SourceType.GITHUB_REVIEW,
                    f"/repos/ethereum/ERCs/pulls/{number}/reviews",
                ),
                (
                    "review-comment",
                    SourceType.GITHUB_REVIEW,
                    f"/repos/ethereum/ERCs/pulls/{number}/comments",
                ),
            ):
                for item in await self._bounded_pages(client, path):
                    item_record = self._github_record(
                        item,
                        kind=kind,
                        source_type=source_type,
                        fallback_url=target.proposal_pr_url,
                        fallback_created=record.created_at if record is not None else now,
                    )
                    if item_record is not None and since <= item_record.updated_at <= now:
                        records.append(item_record)
        return tuple(records)

    async def _bounded_pages(
        self,
        client: GitHubClient,
        path: str,
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for page in range(1, self._MAX_PROPOSAL_PAGES + 1):
            payload = await client.get_json(path, {"per_page": 100, "page": page})
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise DailyEvidenceRejected("proposal PR Daily page is invalid")
            records.extend(payload)
            if len(payload) < 100:
                return records
        raise DailyEvidenceRejected("proposal PR Daily pagination exceeded its budget")

    @staticmethod
    def _github_record(
        payload: dict[str, object],
        *,
        kind: str,
        source_type: SourceType,
        fallback_url: str,
        fallback_created: datetime,
    ) -> SourceRecord | None:
        raw_id = payload.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            return None
        created = _parse_github_timestamp(payload.get("created_at"), fallback_created)
        updated = _parse_github_timestamp(
            payload.get("updated_at") or payload.get("submitted_at"),
            created,
        )
        title = payload.get("title")
        body = payload.get("body")
        text = "\n\n".join(
            value.strip()
            for value in (title, body)
            if isinstance(value, str) and value.strip()
        )
        if not text:
            return None
        user = payload.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        locator = payload.get("html_url")
        return SourceRecord.from_text(
            record_id=github_id("ethereum-ercs", kind, raw_id),
            source_type=source_type,
            source_locator=locator if isinstance(locator, str) else fallback_url,
            author_person_id=None,
            author_source_handle=login if isinstance(login, str) else None,
            created_at=created,
            updated_at=max(created, updated),
            text_original=text,
            ingested_at=updated,
            source_payload={"pull_number": payload.get("number")},
        )


class MagiciansActivityRecords:
    _BASE_URL = "https://ethereum-magicians.org"

    def __init__(
        self,
        root: Path,
        *,
        client: httpx.AsyncClient,
        registry: ScanTargetRegistry,
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
        for target in self.registry.ercs:
            parts = [
                part
                for part in urlparse(target.magicians_topic_url).path.split("/")
                if part
            ]
            topic_id = magicians_topic_id(target.magicians_topic_url)
            by_id[topic_id] = TopicSeed(topic_id, parts[1], "scan_target_registry")
        return tuple(by_id[key] for key in sorted(by_id))


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")


def _parse_github_timestamp(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else fallback


def _log_source_failure(source: str, code: str) -> None:
    print(
        f"tawg_event=phase_failed phase=daily_evidence source={source} code={code}",
        flush=True,
    )
