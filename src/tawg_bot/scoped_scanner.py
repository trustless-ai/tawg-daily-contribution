"""Metadata-only recurring scans confined to controller-owned source targets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator

from tawg_bot.models import SourceCursors, StrictModel
from tawg_bot.scan_targets import (
    ScanGitHubClient,
    ScanTargetStore,
    ScanTopicClient,
    magicians_topic_id,
    proposal_pr_number,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork

_MAX_GITHUB_PAGES = 10


def _index_with_repository(index: str, link: str) -> str:
    """Insert a repository wikilink into the index's Repositories section."""

    if link in index:
        return index
    heading = "## Repositories"
    if heading not in index:
        return f"{index.rstrip()}\n\n{heading}\n\n{link}\n"
    lines = index.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (
            position
            for position in range(start, len(lines))
            if lines[position].startswith("## ")
        ),
        len(lines),
    )
    section = lines[start:end]
    links = sorted({item for item in section if item.startswith("- [[")} | {link})
    remainder = [item for item in section if not item.startswith("- [[")]
    while remainder and not remainder[-1].strip():
        remainder.pop()
    replacement = ["", *links]
    if remainder:
        replacement.extend(["", *remainder])
    lines[start:end] = replacement
    return "\n".join(lines).rstrip() + "\n"


class ScopedScanRejected(ValueError):
    """Raised when scanner inputs or repository state violate the bounded contract."""


class RepositoryDescriptor(StrictModel):
    """The bounded public-repository metadata needed to maintain a repository page."""

    name: str = Field(min_length=1, max_length=256)
    full_name: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=1024)


class ScopedObservation(StrictModel):
    source_key: str = Field(min_length=1, max_length=256)
    source_kind: Literal[
        "github_repository",
        "magicians_topic",
        "ethereum_ercs_pr",
    ]
    source_locator: str = Field(min_length=1, max_length=2048)
    updated_at: datetime
    metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("observation timestamp must use UTC")
        return value.astimezone(UTC)


class ScopedScanResult(StrictModel):
    observations: tuple[ScopedObservation, ...]
    repositories: tuple[RepositoryDescriptor, ...] = ()
    github_cursors: dict[str, str | int | None]
    magicians_cursors: dict[str, str | int | None]
    failed_sources: tuple[str, ...]

    @model_validator(mode="after")
    def observation_keys_are_unique(self) -> ScopedScanResult:
        keys = [
            (observation.source_kind, observation.source_key)
            for observation in self.observations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("scoped observation keys must be unique")
        return self


class ScopedSourceScanner:
    """Observe only all public Trustless-AI repos and explicitly registered ERC sources."""

    OBSERVATIONS_PATH = "data/state/scoped-source-observations.json"
    CURSORS_PATH = "data/state/source-cursors.json"

    def __init__(
        self,
        root: Path,
        *,
        github_client: ScanGitHubClient | None,
        topic_client: ScanTopicClient,
    ) -> None:
        self.root = root.resolve()
        self.github_client = github_client
        self.topic_client = topic_client

    async def scan(self, *, since: datetime, now: datetime) -> ScopedScanResult:
        self._require_utc(since)
        self._require_utc(now)
        if since > now:
            raise ValueError("scoped scan cutoff cannot be in the future")
        registry = ScanTargetStore(self.root).load()
        observations: list[ScopedObservation] = []
        repo_descriptors: list[RepositoryDescriptor] = []
        github_cursors: dict[str, str | int | None] = {}
        magicians_cursors: dict[str, str | int | None] = {}
        failures: list[str] = []

        if self.github_client is None:
            failures.append("github:trustless-ai")
        else:
            try:
                repositories = await self._github_pages(
                    f"/orgs/{registry.github_organization}/repos",
                    {"type": "public", "sort": "full_name", "direction": "asc"},
                )
                for repository in repositories:
                    if repository.get("private") is True:
                        continue
                    observation = self._repository_observation(repository, now=now)
                    observations.append(observation)
                    repo_descriptors.append(self._repository_descriptor(repository))
                    github_cursors[
                        f"repo:{observation.source_key}:metadata_sha256"
                    ] = observation.metadata_sha256
            except Exception:
                failures.append("github:trustless-ai")

        for target in registry.ercs:
            topic_id = magicians_topic_id(target.magicians_topic_url)
            try:
                topic = await self.topic_client.get_json(f"/t/{topic_id}.json")
                observation = self._topic_observation(
                    topic,
                    expected_topic_id=topic_id,
                    source_locator=target.magicians_topic_url,
                    now=now,
                )
                observations.append(observation)
                magicians_cursors[
                    f"topic:{topic_id}:metadata_sha256"
                ] = observation.metadata_sha256
            except Exception:
                failures.append(f"magicians:{topic_id}")

            if target.proposal_pr_url is None:
                continue
            pull_number = proposal_pr_number(target.proposal_pr_url)
            if self.github_client is None:
                failures.append(f"ethereum-ercs-pr:{pull_number}")
                continue
            try:
                observation = await self._proposal_observation(
                    pull_number,
                    source_locator=target.proposal_pr_url,
                    now=now,
                )
                observations.append(observation)
                github_cursors[
                    f"ethereum-ercs-pr:{pull_number}:metadata_sha256"
                ] = observation.metadata_sha256
            except Exception:
                failures.append(f"ethereum-ercs-pr:{pull_number}")

        ordered = tuple(
            sorted(observations, key=lambda item: (item.source_kind, item.source_key))
        )
        return ScopedScanResult(
            observations=ordered,
            repositories=tuple(
                sorted(repo_descriptors, key=lambda item: item.name)
            ),
            github_cursors=github_cursors,
            magicians_cursors=magicians_cursors,
            failed_sources=tuple(sorted(set(failures))),
        )

    def stage(self, result: ScopedScanResult, uow: RepositoryUnitOfWork) -> None:
        if uow.root != self.root:
            raise ScopedScanRejected("scanner and unit of work roots differ")
        cursors = self._load_cursors()
        cursors.github.update(result.github_cursors)
        cursors.magicians.update(result.magicians_cursors)
        uow.stage_json(
            self.OBSERVATIONS_PATH,
            [
                item.model_dump(mode="json")
                for item in self._merged_observations(result)
            ],
        )
        uow.stage_json(self.CURSORS_PATH, cursors.model_dump(mode="json"))

    def stage_repository_pages(
        self,
        uow: RepositoryUnitOfWork,
        repositories: tuple[RepositoryDescriptor, ...],
        now: datetime,
    ) -> None:
        """Backfill a deterministic repository page for each observed repo missing one."""

        if uow.root != self.root:
            raise ScopedScanRejected("scanner and unit of work roots differ")
        self._require_utc(now)
        if not repositories:
            return
        index_path = "knowledge/index.md"
        index_file = self.root / index_path
        if index_file.is_symlink() or not index_file.is_file():
            return
        try:
            index = index_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ScopedScanRejected("repository index is not readable") from None
        new_links: list[str] = []
        for repository in repositories:
            page_stem = repository.name.casefold()
            page_path = f"knowledge/repos/{page_stem}.md"
            if (self.root / page_path).is_file():
                continue
            uow.stage_bytes(
                page_path,
                self._repository_page(repository, now=now).encode("utf-8"),
            )
            new_links.append(f"- [[repos/{page_stem}|{page_stem}]]")
        if not new_links:
            return
        for link in new_links:
            index = _index_with_repository(index, link)
        uow.stage_bytes(index_path, index.encode("utf-8"))

    @staticmethod
    def _repository_page(repository: RepositoryDescriptor, *, now: datetime) -> str:
        title = repository.name.casefold()
        html_url = f"https://github.com/{repository.full_name}"
        purpose = (repository.description or "").strip() or (
            "Repository under the trustless-ai organization."
        )
        date = now.date().isoformat()
        lines = [
            "---",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            "type: repository",
            f"created: {json.dumps(date)}",
            f"updated: {json.dumps(date)}",
            "source_urls:",
            f"- {json.dumps(html_url)}",
            "provenance_status: verified",
            "---",
            "",
            f"# {title}",
            "",
            "## Purpose",
            "",
            purpose,
            "",
            "## Evidence snapshot",
            "",
            f"- Canonical source: [{repository.name}]({html_url})",
            "",
            f"Public repository activity is preserved under "
            f"`data/github/{repository.name}/` for exact retrieval.",
        ]
        return "\n".join(lines).rstrip() + "\n"

    async def _proposal_observation(
        self,
        pull_number: int,
        *,
        source_locator: str,
        now: datetime,
    ) -> ScopedObservation:
        assert self.github_client is not None
        pull = await self.github_client.get_json(
            f"/repos/ethereum/ERCs/pulls/{pull_number}"
        )
        if not isinstance(pull, dict) or pull.get("number") != pull_number:
            raise ScopedScanRejected("proposal PR metadata is invalid")
        streams: dict[str, list[dict[str, Any]]] = {}
        for name, path in (
            ("issue_comments", f"/repos/ethereum/ERCs/issues/{pull_number}/comments"),
            ("reviews", f"/repos/ethereum/ERCs/pulls/{pull_number}/reviews"),
            ("review_comments", f"/repos/ethereum/ERCs/pulls/{pull_number}/comments"),
        ):
            streams[name] = await self._github_pages(path, {})
        metadata = {
            "id": pull.get("id"),
            "number": pull.get("number"),
            "state": pull.get("state"),
            "title": pull.get("title"),
            "updated_at": pull.get("updated_at"),
            "streams": {
                name: [self._activity_metadata(item) for item in items]
                for name, items in streams.items()
            },
        }
        return ScopedObservation(
            source_key=f"ethereum-ercs-pr-{pull_number}",
            source_kind="ethereum_ercs_pr",
            source_locator=source_locator,
            updated_at=self._timestamp_or_now(pull.get("updated_at"), now),
            metadata_sha256=self._metadata_sha256(metadata),
        )

    async def _github_pages(
        self,
        path: str,
        params: dict[str, object],
    ) -> list[dict[str, Any]]:
        assert self.github_client is not None
        items: list[dict[str, Any]] = []
        for page in range(1, _MAX_GITHUB_PAGES + 1):
            payload = await self.github_client.get_json(
                path,
                {**params, "per_page": 100, "page": page},
            )
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise ScopedScanRejected("GitHub metadata page is invalid")
            items.extend(payload)
            if len(payload) < 100:
                return items
        raise ScopedScanRejected("GitHub metadata pagination exceeded its budget")

    @classmethod
    def _repository_observation(
        cls,
        payload: dict[str, Any],
        *,
        now: datetime,
    ) -> ScopedObservation:
        name = payload.get("name")
        full_name = payload.get("full_name")
        default_branch = payload.get("default_branch")
        if (
            not isinstance(name, str)
            or not isinstance(full_name, str)
            or not full_name.startswith("trustless-ai/")
            or not isinstance(default_branch, str)
        ):
            raise ScopedScanRejected("GitHub repository metadata is invalid")
        metadata = {
            "id": payload.get("id"),
            "name": name,
            "full_name": full_name,
            "default_branch": default_branch,
            "archived": payload.get("archived") is True,
            "updated_at": payload.get("updated_at"),
        }
        return ScopedObservation(
            source_key=name,
            source_kind="github_repository",
            source_locator=f"https://github.com/{full_name}",
            updated_at=cls._timestamp_or_now(payload.get("updated_at"), now),
            metadata_sha256=cls._metadata_sha256(metadata),
        )

    @staticmethod
    def _repository_descriptor(payload: dict[str, Any]) -> RepositoryDescriptor:
        name = payload.get("name")
        full_name = payload.get("full_name")
        if not isinstance(name, str) or not isinstance(full_name, str):
            raise ScopedScanRejected("GitHub repository metadata is invalid")
        description = payload.get("description")
        return RepositoryDescriptor(
            name=name,
            full_name=full_name,
            description=description if isinstance(description, str) else None,
        )

    @classmethod
    def _topic_observation(
        cls,
        payload: dict[str, Any],
        *,
        expected_topic_id: int,
        source_locator: str,
        now: datetime,
    ) -> ScopedObservation:
        expected_slug = urlsplit(source_locator).path.split("/")[2]
        if payload.get("id") != expected_topic_id or payload.get("slug") != expected_slug:
            raise ScopedScanRejected("Magicians topic metadata is invalid")
        metadata = {
            "id": payload.get("id"),
            "slug": payload.get("slug"),
            "title": payload.get("title"),
            "posts_count": payload.get("posts_count"),
            "highest_post_number": payload.get("highest_post_number"),
            "last_posted_at": payload.get("last_posted_at"),
        }
        return ScopedObservation(
            source_key=f"magicians-{expected_topic_id}",
            source_kind="magicians_topic",
            source_locator=source_locator,
            updated_at=cls._timestamp_or_now(payload.get("last_posted_at"), now),
            metadata_sha256=cls._metadata_sha256(metadata),
        )

    @staticmethod
    def _activity_metadata(payload: dict[str, Any]) -> dict[str, object]:
        user = payload.get("user")
        return {
            "id": payload.get("id"),
            "state": payload.get("state"),
            "submitted_at": payload.get("submitted_at"),
            "updated_at": payload.get("updated_at"),
            "user": user.get("login") if isinstance(user, dict) else None,
        }

    @staticmethod
    def _metadata_sha256(payload: object) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _timestamp_or_now(value: object, now: datetime) -> datetime:
        if not isinstance(value, str):
            return now
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return now
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else now

    def _load_cursors(self) -> SourceCursors:
        path = self.root / self.CURSORS_PATH
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("source cursor state is not a regular file")
            return SourceCursors.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as error:
            raise ScopedScanRejected("source cursor state is invalid") from error

    def _merged_observations(
        self,
        result: ScopedScanResult,
    ) -> tuple[ScopedObservation, ...]:
        path = self.root / self.OBSERVATIONS_PATH
        if not path.exists():
            existing: tuple[ScopedObservation, ...] = ()
        else:
            if path.is_symlink() or not path.is_file():
                raise ScopedScanRejected("scoped observation state is invalid")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError
                existing = tuple(ScopedObservation.model_validate(item) for item in raw)
            except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError):
                raise ScopedScanRejected("scoped observation state is invalid") from None
        failures = set(result.failed_sources)
        retained: dict[tuple[str, str], ScopedObservation] = {}
        for item in existing:
            keep = (
                item.source_kind == "github_repository"
                and "github:trustless-ai" in failures
            ) or (
                item.source_kind == "magicians_topic"
                and f"magicians:{item.source_key.rsplit('-', 1)[-1]}" in failures
            ) or (
                item.source_kind == "ethereum_ercs_pr"
                and f"ethereum-ercs-pr:{item.source_key.rsplit('-', 1)[-1]}"
                in failures
            )
            if keep:
                retained[(item.source_kind, item.source_key)] = item
        for item in result.observations:
            retained[(item.source_kind, item.source_key)] = item
        return tuple(
            retained[key]
            for key in sorted(retained, key=lambda value: (value[0], value[1]))
        )

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("scoped scan timestamps must use UTC")
