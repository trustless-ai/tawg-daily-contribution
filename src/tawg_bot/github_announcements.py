"""Deterministic GitHub activity announcements for the TAWG Telegram group."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from tawg_bot.github_source import GitHubSourceError
from tawg_bot.models import StrictModel
from tawg_bot.unit_of_work import RepositoryUnitOfWork

_REPOSITORY = re.compile(r"^trustless-ai/[A-Za-z0-9_.-]{1,100}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40,64}$")
_MAX_GITHUB_PAGES = 10
_PULL_URL_REFERENCE = re.compile(
    r"https://github\.com/(trustless-ai/[A-Za-z0-9_.-]{1,100})/pull/"
    r"([1-9][0-9]{0,9})(?:\b|/)",
    re.IGNORECASE,
)
_PULL_SHORTHAND_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:(trustless-ai)/)?"
    r"([A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,9})\b",
    re.IGNORECASE,
)


class GitHubAnnouncementRejected(ValueError):
    """Raised when GitHub metadata or durable announcement state is incomplete."""


class GitHubAnnouncementClient(Protocol):
    async def get_json(
        self,
        path: str,
        params: dict[str, object] | None = None,
    ) -> list[Any] | dict[str, Any]: ...


class PublicGitHubHttpClient:
    """Unauthenticated, read-only GitHub client used only for local baselining."""

    def __init__(self, *, client: httpx.AsyncClient) -> None:
        self.client = client
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    async def get_json(
        self,
        path: str,
        params: dict[str, object] | None = None,
    ) -> list[Any] | dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubAnnouncementRejected("GitHub API path is invalid")
        try:
            response = await self.client.get(
                f"https://api.github.com{path}",
                params=cast(Any, params),
                headers=self.headers,
            )
        except httpx.HTTPError:
            raise GitHubAnnouncementRejected("GitHub bootstrap request failed") from None
        if not response.is_success:
            raise GitHubAnnouncementRejected("GitHub bootstrap request was rejected")
        try:
            payload = response.json()
        except ValueError:
            raise GitHubAnnouncementRejected("GitHub bootstrap response was invalid") from None
        if not isinstance(payload, list | dict):
            raise GitHubAnnouncementRejected("GitHub bootstrap response was invalid")
        return payload


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("GitHub announcement timestamps must use UTC")
    return value.astimezone(UTC)


class GitHubAnnouncementKind(StrEnum):
    PR_OPENED = "pr_opened"
    PR_UPDATED = "pr_updated"
    PR_MERGED = "pr_merged"
    ISSUE_OPENED = "issue_opened"


class GitHubPullSnapshot(StrictModel):
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    author_login: str = Field(pattern=_LOGIN.pattern)
    head_sha: str = Field(pattern=_GIT_SHA.pattern)
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None
    merge_commit_sha: str | None = Field(default=None, pattern=_GIT_SHA.pattern)

    _created_at_utc = field_validator("created_at")(_require_utc)
    _updated_at_utc = field_validator("updated_at")(_require_utc)

    @field_validator("merged_at")
    @classmethod
    def merged_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def merge_fields_are_consistent(self) -> GitHubPullSnapshot:
        if (self.merged_at is None) != (self.merge_commit_sha is None):
            raise ValueError("merged PR metadata must include both merge fields")
        return self


class GitHubIssueSnapshot(StrictModel):
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    author_login: str = Field(pattern=_LOGIN.pattern)
    created_at: datetime
    updated_at: datetime

    _created_at_utc = field_validator("created_at")(_require_utc)
    _updated_at_utc = field_validator("updated_at")(_require_utc)


class GitHubRepositorySnapshot(StrictModel):
    full_name: str = Field(pattern=_REPOSITORY.pattern)
    open_pulls: tuple[GitHubPullSnapshot, ...] = ()
    recently_closed_pulls: tuple[GitHubPullSnapshot, ...] = ()
    open_issues: tuple[GitHubIssueSnapshot, ...] = ()

    @model_validator(mode="after")
    def item_numbers_are_unique(self) -> GitHubRepositorySnapshot:
        for items in (self.open_pulls, self.recently_closed_pulls, self.open_issues):
            numbers = [item.number for item in items]
            if len(numbers) != len(set(numbers)):
                raise ValueError("GitHub repository snapshot item numbers must be unique")
        return self


class GitHubScanSnapshot(StrictModel):
    repositories: tuple[GitHubRepositorySnapshot, ...]

    @model_validator(mode="after")
    def repository_names_are_unique(self) -> GitHubScanSnapshot:
        names = [item.full_name for item in self.repositories]
        if len(names) != len(set(names)):
            raise ValueError("GitHub repository snapshots must be unique")
        return self


class GitHubPullState(StrictModel):
    number: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    author_login: str | None = Field(default=None, pattern=_LOGIN.pattern)
    head_sha: str = Field(pattern=_GIT_SHA.pattern)
    state: Literal["open", "closed", "merged"] | None = None
    updated_at: datetime | None = None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = Field(default=None, pattern=_GIT_SHA.pattern)

    @field_validator("updated_at", "merged_at")
    @classmethod
    def optional_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def current_metadata_is_consistent(self) -> GitHubPullState:
        metadata = (self.title, self.author_login, self.state, self.updated_at)
        if any(value is not None for value in metadata) and any(
            value is None for value in metadata
        ):
            raise ValueError("current PR metadata must be complete")
        if self.state == "merged":
            if self.merged_at is None or self.merge_commit_sha is None:
                raise ValueError("merged current PR metadata is incomplete")
        elif self.state in {"open", "closed"} and (
            self.merged_at is not None or self.merge_commit_sha is not None
        ):
            raise ValueError("unmerged current PR metadata includes merge fields")
        return self

    def context_evidence(
        self,
        *,
        repository: str,
        checked_at: datetime,
    ) -> dict[str, object] | None:
        if (
            self.title is None
            or self.author_login is None
            or self.state is None
            or self.updated_at is None
        ):
            return None
        evidence: dict[str, object] = {
            "kind": "github_pull_current_state",
            "repository": repository,
            "number": self.number,
            "title": self.title,
            "author_login": self.author_login,
            "state": self.state,
            "head_sha": self.head_sha,
            "updated_at": self.updated_at.isoformat().replace("+00:00", "Z"),
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "url": f"https://github.com/{repository}/pull/{self.number}",
        }
        if self.merged_at is not None:
            evidence["merged_at"] = self.merged_at.isoformat().replace("+00:00", "Z")
        return evidence


class GitHubRepositoryState(StrictModel):
    full_name: str = Field(pattern=_REPOSITORY.pattern)
    initialized_at: datetime
    checked_at: datetime
    pulls: tuple[GitHubPullState, ...] = ()
    issue_numbers: tuple[int, ...] = ()

    _initialized_at_utc = field_validator("initialized_at")(_require_utc)
    _checked_at_utc = field_validator("checked_at")(_require_utc)

    @model_validator(mode="after")
    def state_keys_are_unique(self) -> GitHubRepositoryState:
        pull_numbers = [item.number for item in self.pulls]
        if len(pull_numbers) != len(set(pull_numbers)):
            raise ValueError("GitHub PR state numbers must be unique")
        if len(self.issue_numbers) != len(set(self.issue_numbers)):
            raise ValueError("GitHub issue state numbers must be unique")
        return self


class GitHubAnnouncementState(StrictModel):
    schema_version: Literal["tawg.github-announcement-state.v1"] = (
        "tawg.github-announcement-state.v1"
    )
    initialized_at: datetime
    repositories: tuple[GitHubRepositoryState, ...]

    _initialized_at_utc = field_validator("initialized_at")(_require_utc)

    @model_validator(mode="after")
    def repository_state_names_are_unique(self) -> GitHubAnnouncementState:
        names = [item.full_name for item in self.repositories]
        if len(names) != len(set(names)):
            raise ValueError("GitHub repository state names must be unique")
        return self


class GitHubAnnouncement(StrictModel):
    schema_version: Literal["tawg.github-announcement.v1"] = "tawg.github-announcement.v1"
    event_id: str = Field(pattern=r"^github-announcement:[a-z_]+:[a-f0-9]{24}$")
    kind: GitHubAnnouncementKind
    repository: str = Field(pattern=_REPOSITORY.pattern)
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    author_login: str = Field(pattern=_LOGIN.pattern)
    occurred_at: datetime

    _occurred_at_utc = field_validator("occurred_at")(_require_utc)


class GitHubReconcileResult(StrictModel):
    state: GitHubAnnouncementState
    events: tuple[GitHubAnnouncement, ...]


class GitHubAnnouncementBatch(StrictModel):
    state: GitHubAnnouncementState
    pending: tuple[GitHubAnnouncement, ...]


class GitHubAnnouncementScanner:
    """Fetch public organization metadata and stage a retry-safe announcement queue."""

    STATE_PATH = "data/state/github-announcement-state.json"
    PENDING_PATH = "data/state/pending-github-announcements.json"

    def __init__(
        self,
        root: Path,
        *,
        client: GitHubAnnouncementClient | None,
    ) -> None:
        self.root = root.resolve()
        self.client = client

    async def bootstrap(self, *, now: datetime) -> GitHubAnnouncementBatch:
        """Fetch only open items and create a silent first baseline."""

        _require_utc(now)
        if self.client is None:
            raise GitHubAnnouncementRejected("GitHub client is unavailable")
        if (self.root / self.STATE_PATH).exists() or (self.root / self.PENDING_PATH).exists():
            raise GitHubAnnouncementRejected("GitHub announcement baseline already exists")
        try:
            snapshot = await self._snapshot(previous=None)
            state = bootstrap_announcement_state(snapshot, now=now)
        except GitHubAnnouncementRejected:
            raise
        except Exception:
            raise GitHubAnnouncementRejected("GitHub announcement bootstrap incomplete") from None
        return GitHubAnnouncementBatch(state=state, pending=())

    async def scan(self, *, now: datetime) -> GitHubAnnouncementBatch:
        """Fetch one complete L2 snapshot and merge new events into the durable queue."""

        _require_utc(now)
        if self.client is None:
            raise GitHubAnnouncementRejected("GitHub client is unavailable")
        try:
            previous = self._load_state()
            snapshot = await self._snapshot(previous=previous)
            reconciled = reconcile_announcements(previous, snapshot, now=now)
            pending = self._merge_pending(self.pending(), reconciled.events)
            return GitHubAnnouncementBatch(state=reconciled.state, pending=pending)
        except GitHubAnnouncementRejected:
            raise
        except Exception:
            raise GitHubAnnouncementRejected("GitHub announcement scan incomplete") from None

    def stage(
        self,
        batch: GitHubAnnouncementBatch,
        uow: RepositoryUnitOfWork,
    ) -> None:
        if uow.root != self.root:
            raise GitHubAnnouncementRejected("scanner and unit of work roots differ")
        uow.stage_json(self.STATE_PATH, batch.state.model_dump(mode="json"))
        uow.stage_json(
            self.PENDING_PATH,
            [item.model_dump(mode="json") for item in batch.pending],
        )

    def pending(self) -> tuple[GitHubAnnouncement, ...]:
        path = self.root / self.PENDING_PATH
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            pending = tuple(GitHubAnnouncement.model_validate(item) for item in raw)
        except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError):
            raise GitHubAnnouncementRejected(
                "pending GitHub announcement state is invalid"
            ) from None
        if len({item.event_id for item in pending}) != len(pending):
            raise GitHubAnnouncementRejected("pending GitHub announcements are duplicated")
        return pending

    def acknowledge(self, event_id: str) -> None:
        pending = self.pending()
        remaining = tuple(item for item in pending if item.event_id != event_id)
        if len(remaining) == len(pending):
            return
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
        uow = RepositoryUnitOfWork(
            self.root,
            operation_id=f"github-announcement-ack:{digest}",
        )
        uow.register_external_evidence(())
        uow.stage_json(
            self.PENDING_PATH,
            [item.model_dump(mode="json") for item in remaining],
        )
        uow.publish()

    async def _snapshot(
        self,
        *,
        previous: GitHubAnnouncementState | None,
    ) -> GitHubScanSnapshot:
        repositories = await self._pages(
            "/orgs/trustless-ai/repos",
            {"type": "public", "sort": "full_name", "direction": "asc"},
        )
        previous_by_name = (
            {} if previous is None else {item.full_name: item for item in previous.repositories}
        )
        snapshots: list[GitHubRepositorySnapshot] = []
        for raw in repositories:
            full_name = self._repository_name(raw)
            if full_name is None:
                continue
            open_pulls = tuple(
                self._pull(item, expected_state="open")
                for item in await self._pages(
                    f"/repos/{full_name}/pulls",
                    {"state": "open", "sort": "updated", "direction": "asc"},
                )
            )
            open_issues = tuple(
                self._issue(item)
                for item in await self._pages(
                    f"/repos/{full_name}/issues",
                    {"state": "open", "sort": "created", "direction": "asc"},
                )
                if "pull_request" not in item
            )
            old = previous_by_name.get(full_name)
            recently_closed = (
                ()
                if old is None
                else tuple(await self._recently_closed_pulls(full_name, since=old.checked_at))
            )
            snapshots.append(
                GitHubRepositorySnapshot(
                    full_name=full_name,
                    open_pulls=open_pulls,
                    recently_closed_pulls=recently_closed,
                    open_issues=open_issues,
                )
            )
        return GitHubScanSnapshot(repositories=tuple(snapshots))

    async def _recently_closed_pulls(
        self,
        full_name: str,
        *,
        since: datetime,
    ) -> list[GitHubPullSnapshot]:
        assert self.client is not None
        boundary = since.replace(microsecond=0)
        recent: list[GitHubPullSnapshot] = []
        for page in range(1, _MAX_GITHUB_PAGES + 1):
            payload = await self.client.get_json(
                f"/repos/{full_name}/pulls",
                {
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            items = self._metadata_page(payload)
            parsed = [self._pull(item, expected_state="closed") for item in items]
            # GitHub timestamps are only precise to the second, while the scheduler's
            # cursor may contain microseconds. Include the boundary second and rely on
            # durable event identifiers/state to deduplicate it on the next scan.
            recent.extend(item for item in parsed if item.updated_at >= boundary)
            if len(items) < 100 or (parsed and parsed[-1].updated_at < boundary):
                return recent
        raise GitHubAnnouncementRejected("GitHub closed PR pagination exceeded its budget")

    async def _pages(
        self,
        path: str,
        params: dict[str, object],
    ) -> list[dict[str, Any]]:
        assert self.client is not None
        items: list[dict[str, Any]] = []
        for page in range(1, _MAX_GITHUB_PAGES + 1):
            payload = await self.client.get_json(
                path,
                {**params, "per_page": 100, "page": page},
            )
            current = self._metadata_page(payload)
            items.extend(current)
            if len(current) < 100:
                return items
        raise GitHubAnnouncementRejected("GitHub pagination exceeded its budget")

    @staticmethod
    def _metadata_page(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise GitHubAnnouncementRejected("GitHub announcement scan incomplete")
        return payload

    @staticmethod
    def _repository_name(payload: Mapping[str, object]) -> str | None:
        if payload.get("private") is True:
            return None
        full_name = payload.get("full_name")
        if not isinstance(full_name, str) or _REPOSITORY.fullmatch(full_name) is None:
            raise GitHubAnnouncementRejected("GitHub repository metadata is invalid")
        return full_name

    @staticmethod
    def _pull(
        payload: Mapping[str, object],
        *,
        expected_state: Literal["open", "closed"],
    ) -> GitHubPullSnapshot:
        user = payload.get("user")
        head = payload.get("head")
        if (
            payload.get("state") != expected_state
            or not isinstance(user, Mapping)
            or not isinstance(head, Mapping)
        ):
            raise GitHubAnnouncementRejected("GitHub PR metadata is invalid")
        merged_at = _timestamp(payload.get("merged_at"), optional=True)
        merge_commit_sha = payload.get("merge_commit_sha") if merged_at is not None else None
        if merged_at is not None and merge_commit_sha is None:
            merge_commit_sha = head.get("sha")
        try:
            return GitHubPullSnapshot(
                number=payload.get("number"),
                title=payload.get("title"),
                author_login=user.get("login"),
                head_sha=head.get("sha"),
                created_at=_timestamp(payload.get("created_at")),
                updated_at=_timestamp(payload.get("updated_at")),
                merged_at=merged_at,
                merge_commit_sha=merge_commit_sha,
            )
        except (TypeError, ValidationError, ValueError):
            raise GitHubAnnouncementRejected("GitHub PR metadata is invalid") from None

    @staticmethod
    def _issue(payload: Mapping[str, object]) -> GitHubIssueSnapshot:
        user = payload.get("user")
        if payload.get("state") != "open" or not isinstance(user, Mapping):
            raise GitHubAnnouncementRejected("GitHub issue metadata is invalid")
        try:
            return GitHubIssueSnapshot(
                number=payload.get("number"),
                title=payload.get("title"),
                author_login=user.get("login"),
                created_at=_timestamp(payload.get("created_at")),
                updated_at=_timestamp(payload.get("updated_at")),
            )
        except (TypeError, ValidationError, ValueError):
            raise GitHubAnnouncementRejected("GitHub issue metadata is invalid") from None

    def _load_state(self) -> GitHubAnnouncementState:
        path = self.root / self.STATE_PATH
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            return GitHubAnnouncementState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            raise GitHubAnnouncementRejected(
                "GitHub announcement baseline state is invalid"
            ) from None

    @staticmethod
    def _merge_pending(
        existing: tuple[GitHubAnnouncement, ...],
        new: tuple[GitHubAnnouncement, ...],
    ) -> tuple[GitHubAnnouncement, ...]:
        by_id = {item.event_id: item for item in existing}
        for item in new:
            previous = by_id.get(item.event_id)
            if previous is not None and previous != item:
                raise GitHubAnnouncementRejected("GitHub announcement event ID conflicts")
            by_id[item.event_id] = item
        return tuple(
            sorted(
                by_id.values(),
                key=lambda item: (
                    item.occurred_at,
                    item.repository,
                    item.number,
                    item.kind.value,
                ),
            )
        )


async def resolve_referenced_pull_evidence(
    root: Path,
    texts: Iterable[str],
    *,
    now: datetime,
    client: GitHubAnnouncementClient | None,
    max_items: int = 16,
    max_age: timedelta = timedelta(minutes=35),
    refresh_timeout_seconds: float = 8.0,
) -> tuple[dict[str, object], ...]:
    """Use fresh L2 state, with a bounded per-reference refresh when needed."""

    _require_utc(now)
    if max_age <= timedelta(0):
        raise ValueError("current GitHub evidence max age must be positive")
    if not math.isfinite(refresh_timeout_seconds) or refresh_timeout_seconds <= 0:
        raise ValueError("current GitHub refresh timeout must be positive and finite")
    scoped_texts = tuple(texts)
    if not any(
        _PULL_URL_REFERENCE.search(text) or _PULL_SHORTHAND_REFERENCE.search(text)
        for text in scoped_texts
    ):
        return ()
    state = _load_reference_state(root)
    if state is None:
        return ()
    references, reference_limit_exceeded = _referenced_pull_keys(
        state,
        scoped_texts,
        max_items=max_items,
    )
    repositories = {item.full_name: item for item in state.repositories}
    resolved: dict[tuple[str, int], dict[str, object]] = {}
    refreshes: list[tuple[str, int]] = []
    for repository_name, number in references:
        repository = repositories[repository_name]
        pull = next((item for item in repository.pulls if item.number == number), None)
        current = (
            None
            if pull is None
            else pull.context_evidence(
                repository=repository_name,
                checked_at=repository.checked_at,
            )
        )
        if current is not None and repository.checked_at >= now - max_age:
            resolved[(repository_name, number)] = current
            continue
        refreshes.append((repository_name, number))
    if refreshes:
        tasks = [
            asyncio.create_task(
                _refresh_pull_evidence(
                    client,
                    repository=repository_name,
                    number=number,
                    now=now,
                )
            )
            for repository_name, number in refreshes
        ]
        try:
            done, _ = await asyncio.wait(tasks, timeout=refresh_timeout_seconds)
            for key, task in zip(refreshes, tasks, strict=True):
                if task not in done:
                    continue
                refreshed = task.result()
                if refreshed is not None:
                    resolved[key] = refreshed
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    evidence: list[dict[str, object]] = []
    for repository_name, number in references:
        current = resolved.get((repository_name, number))
        if current is not None:
            evidence.append(current)
            continue
        repository = repositories[repository_name]
        evidence.append(
            {
                "kind": "github_pull_current_state_gap",
                "repository": repository_name,
                "number": number,
                "checked_at": repository.checked_at.isoformat().replace("+00:00", "Z"),
                "reason": "l2_state_stale_or_incomplete",
            }
        )
    if reference_limit_exceeded:
        evidence.append(
            {
                "kind": "github_pull_current_state_coverage_gap",
                "max_items": max_items,
                "reason": "reference_limit_exceeded",
            }
        )
    return tuple(evidence)


def _load_reference_state(root: Path) -> GitHubAnnouncementState | None:
    path = root.resolve() / GitHubAnnouncementScanner.STATE_PATH
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        return GitHubAnnouncementState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        raise GitHubAnnouncementRejected(
            "GitHub announcement baseline state is invalid"
        ) from None


def _referenced_pull_keys(
    state: GitHubAnnouncementState,
    texts: Iterable[str],
    *,
    max_items: int,
) -> tuple[tuple[tuple[str, int], ...], bool]:
    if max_items <= 0:
        raise ValueError("current GitHub evidence limit must be positive")
    repositories = {item.full_name: item for item in state.repositories}
    short_names: dict[str, str | None] = {}
    for full_name in repositories:
        short = full_name.removeprefix("trustless-ai/").casefold()
        short_names[short] = full_name if short not in short_names else None
    references: set[tuple[str, int]] = set()
    ordered_references: list[tuple[str, int]] = []
    reference_limit_exceeded = False
    canonical_names = {name.casefold(): name for name in repositories}
    for text in texts:
        matches: list[tuple[int, str | None, int]] = []
        matches.extend(
            (match.start(), canonical_names.get(match.group(1).casefold()), int(match.group(2)))
            for match in _PULL_URL_REFERENCE.finditer(text)
        )
        for match in _PULL_SHORTHAND_REFERENCE.finditer(text):
            canonical = (
                canonical_names.get(f"trustless-ai/{match.group(2)}".casefold())
                if match.group(1) is not None
                else short_names.get(match.group(2).casefold())
            )
            matches.append((match.start(), canonical, int(match.group(3))))
        for _, canonical, number in sorted(matches, key=lambda item: item[0]):
            if canonical is None or (canonical, number) in references:
                continue
            reference = (canonical, number)
            references.add(reference)
            if len(ordered_references) < max_items:
                ordered_references.append(reference)
            else:
                reference_limit_exceeded = True
    return tuple(ordered_references), reference_limit_exceeded


async def _refresh_pull_evidence(
    client: GitHubAnnouncementClient | None,
    *,
    repository: str,
    number: int,
    now: datetime,
) -> dict[str, object] | None:
    if client is None:
        return None
    try:
        payload = await client.get_json(f"/repos/{repository}/pulls/{number}")
        if not isinstance(payload, Mapping) or payload.get("state") not in {"open", "closed"}:
            raise GitHubAnnouncementRejected("GitHub PR metadata is invalid")
        snapshot = GitHubAnnouncementScanner._pull(
            payload,
            expected_state=cast(Literal["open", "closed"], payload["state"]),
        )
        state: Literal["open", "closed", "merged"] = (
            "merged"
            if snapshot.merged_at is not None
            else cast(Literal["open", "closed"], payload["state"])
        )
        return _pull_state(snapshot, state=state).context_evidence(
            repository=repository,
            checked_at=now,
        )
    except (GitHubAnnouncementRejected, GitHubSourceError):
        return None


def bootstrap_announcement_state(
    snapshot: GitHubScanSnapshot,
    *,
    now: datetime,
) -> GitHubAnnouncementState:
    """Build a silent baseline from only currently open PRs and issues."""

    _require_utc(now)
    repositories = tuple(
        _baseline_repository(item, now=now)
        for item in sorted(snapshot.repositories, key=lambda value: value.full_name)
    )
    return GitHubAnnouncementState(initialized_at=now, repositories=repositories)


def reconcile_announcements(
    previous: GitHubAnnouncementState,
    snapshot: GitHubScanSnapshot,
    *,
    now: datetime,
) -> GitHubReconcileResult:
    """Advance durable state and return at most one deterministic event per item."""

    _require_utc(now)
    previous_by_name = {item.full_name: item for item in previous.repositories}
    repositories: list[GitHubRepositoryState] = []
    events: list[GitHubAnnouncement] = []
    for current in sorted(snapshot.repositories, key=lambda item: item.full_name):
        old = previous_by_name.get(current.full_name)
        if old is None:
            repositories.append(_baseline_repository(current, now=now))
            continue
        updated, repository_events = _reconcile_repository(old, current, now=now)
        repositories.append(updated)
        events.extend(repository_events)
    ordered_events = tuple(
        sorted(
            events,
            key=lambda item: (
                item.occurred_at,
                item.repository,
                item.number,
                item.kind.value,
            ),
        )
    )
    return GitHubReconcileResult(
        state=GitHubAnnouncementState(
            initialized_at=previous.initialized_at,
            repositories=tuple(repositories),
        ),
        events=ordered_events,
    )


def _baseline_repository(
    current: GitHubRepositorySnapshot,
    *,
    now: datetime,
) -> GitHubRepositoryState:
    return GitHubRepositoryState(
        full_name=current.full_name,
        initialized_at=now,
        checked_at=now,
        pulls=tuple(
            _pull_state(item, state="open")
            for item in sorted(current.open_pulls, key=lambda value: value.number)
        ),
        issue_numbers=tuple(sorted(item.number for item in current.open_issues)),
    )


def _reconcile_repository(
    previous: GitHubRepositoryState,
    current: GitHubRepositorySnapshot,
    *,
    now: datetime,
) -> tuple[GitHubRepositoryState, tuple[GitHubAnnouncement, ...]]:
    pulls = {item.number: item for item in previous.pulls}
    candidates: dict[int, GitHubAnnouncement] = {}
    boundary = previous.checked_at.replace(microsecond=0)
    for item in current.open_pulls:
        old = pulls.get(item.number)
        if old is None and item.created_at >= boundary:
            candidates[item.number] = _announcement(
                GitHubAnnouncementKind.PR_OPENED,
                current.full_name,
                item,
                occurred_at=item.created_at,
                discriminator=item.head_sha,
            )
        elif old is not None and old.head_sha != item.head_sha:
            candidates[item.number] = _announcement(
                GitHubAnnouncementKind.PR_UPDATED,
                current.full_name,
                item,
                occurred_at=item.updated_at,
                discriminator=item.head_sha,
            )
        pulls[item.number] = _pull_state(item, state="open")

    for item in current.recently_closed_pulls:
        old = pulls.get(item.number)
        pulls[item.number] = _pull_state(
            item,
            state="merged" if item.merged_at is not None else "closed",
        )
        candidates.pop(item.number, None)
        if (
            item.merged_at is not None
            and item.merge_commit_sha is not None
            and item.merged_at >= boundary
            and (old is None or old.merge_commit_sha is None)
        ):
            candidates[item.number] = _announcement(
                GitHubAnnouncementKind.PR_MERGED,
                current.full_name,
                item,
                occurred_at=item.merged_at,
                discriminator=item.merge_commit_sha,
            )

    issue_numbers = set(previous.issue_numbers)
    issue_events: list[GitHubAnnouncement] = []
    for issue_item in current.open_issues:
        if issue_item.number not in issue_numbers and issue_item.created_at >= boundary:
            issue_events.append(
                _announcement(
                    GitHubAnnouncementKind.ISSUE_OPENED,
                    current.full_name,
                    issue_item,
                    occurred_at=issue_item.created_at,
                    discriminator=str(issue_item.number),
                )
            )
        issue_numbers.add(issue_item.number)

    state = GitHubRepositoryState(
        full_name=current.full_name,
        initialized_at=previous.initialized_at,
        checked_at=now,
        pulls=tuple(pulls[number] for number in sorted(pulls)),
        issue_numbers=tuple(sorted(issue_numbers)),
    )
    return state, (*candidates.values(), *issue_events)


def _pull_state(
    item: GitHubPullSnapshot,
    *,
    state: Literal["open", "closed", "merged"],
) -> GitHubPullState:
    return GitHubPullState(
        number=item.number,
        title=item.title,
        author_login=item.author_login,
        head_sha=item.head_sha,
        state=state,
        updated_at=item.updated_at,
        merged_at=item.merged_at,
        merge_commit_sha=item.merge_commit_sha,
    )


def _announcement(
    kind: GitHubAnnouncementKind,
    repository: str,
    item: GitHubPullSnapshot | GitHubIssueSnapshot,
    *,
    occurred_at: datetime,
    discriminator: str,
) -> GitHubAnnouncement:
    key = json.dumps(
        [kind.value, repository, item.number, discriminator],
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:24]
    return GitHubAnnouncement(
        event_id=f"github-announcement:{kind.value}:{digest}",
        kind=kind,
        repository=repository,
        number=item.number,
        title=item.title,
        author_login=item.author_login,
        occurred_at=occurred_at,
    )


def render_announcement(event: GitHubAnnouncement) -> str:
    """Render one safe, fixed Telegram Rich Markdown notification."""

    repository = _markdown_label(event.repository)
    title = _markdown_label(f"#{event.number} {_single_line(event.title)}")
    author = _markdown_label(event.author_login)
    repository_url = f"https://github.com/{event.repository}"
    item_kind = "pull" if event.kind is not GitHubAnnouncementKind.ISSUE_OPENED else "issues"
    item_url = f"{repository_url}/{item_kind}/{event.number}"
    author_url = f"https://github.com/{quote(event.author_login, safe='')}"
    heading, action = {
        GitHubAnnouncementKind.PR_OPENED: ("🆕 **New PR**", "Opened by"),
        GitHubAnnouncementKind.PR_UPDATED: ("🔄 **PR updated**", "New commits from"),
        GitHubAnnouncementKind.PR_MERGED: ("🎉 **PR merged**", "Congratulations,"),
        GitHubAnnouncementKind.ISSUE_OPENED: ("🆕 **New issue**", "Opened by"),
    }[event.kind]
    suffix = "!" if event.kind is GitHubAnnouncementKind.PR_MERGED else ""
    return (
        f"{heading} in [{repository}]({repository_url})\n"
        f"[{title}]({item_url})\n"
        f"{action} [{author}]({author_url}){suffix}"
    )


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _markdown_label(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "[]()_*`~<>&":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _timestamp(value: object, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("GitHub timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("GitHub timestamp is invalid") from None
    return _require_utc(parsed)
