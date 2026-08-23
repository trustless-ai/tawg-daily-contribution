"""Incremental collection of public evidence from a GitHub organization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx

from tawg_bot.ids import github_id
from tawg_bot.models import Relation, SourceCursors, SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.source_filters import GitHubPathPolicy


class GitHubSourceError(RuntimeError):
    """A source failure with no credential-bearing request details."""


class GitHubClient(Protocol):
    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> list[Any] | dict[str, Any]: ...

    async def graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]: ...


class GitHubHttpClient:
    def __init__(self, *, token: str, client: httpx.AsyncClient) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._client = client
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient) -> GitHubHttpClient:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise GitHubSourceError("GITHUB_TOKEN is not configured")
        return cls(token=token, client=client)

    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> list[Any] | dict[str, Any]:
        return await self._request("GET", f"https://api.github.com{path}", params=params)

    async def graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise GitHubSourceError("GitHub GraphQL returned an invalid response")
        if payload.get("errors"):
            raise GitHubSourceError("GitHub GraphQL returned errors")
        return cast(dict[str, Any], payload["data"])

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
    ) -> list[Any] | dict[str, Any]:
        try:
            response = await self._client.request(
                method, url, params=cast(Any, params), json=json, headers=self._headers
            )
        except httpx.HTTPError:
            raise GitHubSourceError("GitHub HTTP request failed") from None
        if not response.is_success:
            raise GitHubSourceError(f"GitHub HTTP request returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise GitHubSourceError("GitHub HTTP response was not JSON") from None
        if not isinstance(payload, list | dict):
            raise GitHubSourceError("GitHub HTTP response had an invalid shape")
        return payload


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    name: str
    full_name: str
    default_branch: str
    archived: bool


@dataclass(frozen=True, slots=True)
class GitHubBatch:
    records: tuple[SourceRecord, ...]
    cursors: dict[str, str | int | None]
    failed_streams: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return not self.failed_streams


_DISCUSSIONS_QUERY = """
query TawgDiscussions($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: ASC}) {
      nodes {
        id number title body url createdAt updatedAt author { login }
        comments(first: 100) {
          nodes { id body url createdAt updatedAt author { login } }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_RECENT_DISCUSSIONS_QUERY = (
    _DISCUSSIONS_QUERY.replace("direction: ASC", "direction: DESC")
    .replace("comments(first: 100)", "comments(last: 100)")
    .replace(
        "pageInfo { hasNextPage endCursor }",
        "pageInfo { hasPreviousPage startCursor }",
        1,
    )
)

_RECENT_DISCUSSION_COMMENTS_QUERY = """
query TawgRecentDiscussionComments(
  $owner: String!, $name: String!, $number: Int!, $before: String
) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      comments(last: 100, before: $before) {
        nodes { id body url createdAt updatedAt author { login } }
        pageInfo { hasPreviousPage startCursor }
      }
    }
  }
}
"""

_MAX_ACTIVITY_PAGES = 10
_MAX_ACTIVITY_REQUESTS = 600


@dataclass(slots=True)
class _ActivityRequestBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise GitHubSourceError("GitHub activity exceeded its request budget")
        self.remaining -= 1


_DISCUSSION_COMMENTS_QUERY = """
query TawgDiscussionComments(
  $owner: String!, $name: String!, $number: Int!, $after: String
) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      comments(first: 100, after: $after) {
        nodes { id body url createdAt updatedAt author { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class GitHubSource:
    def __init__(
        self,
        *,
        client: GitHubClient,
        organization: str,
        privacy: PrivacyFilter,
        now: Callable[[], datetime] | None = None,
        path_policy: GitHubPathPolicy | None = None,
    ) -> None:
        self.client = client
        self.organization = organization
        self.now = now or (lambda: datetime.now(UTC))
        self.privacy = privacy
        self.path_policy = path_policy or GitHubPathPolicy()

    @classmethod
    def for_repository(
        cls,
        *,
        root: Path,
        client: GitHubClient,
        organization: str = "trustless-ai",
        now: Callable[[], datetime] | None = None,
    ) -> GitHubSource:
        return cls(
            client=client,
            organization=organization,
            now=now,
            privacy=PrivacyFilter.from_yaml(root / "config/privacy.yml"),
        )

    async def list_public_repositories(
        self, *, request_budget: _ActivityRequestBudget | None = None
    ) -> list[RepositoryRef]:
        items = await self._paginate(
            f"/orgs/{self.organization}/repos",
            {"type": "public", "sort": "full_name", "direction": "asc"},
            request_budget=request_budget,
        )
        repositories: list[RepositoryRef] = []
        for item in items:
            if item.get("private") is True:
                continue
            name = self._require_string(item.get("name"), "repository name")
            full_name = self._require_string(item.get("full_name"), "repository full name")
            default_branch = self._require_string(
                item.get("default_branch"), "repository default branch"
            )
            repositories.append(
                RepositoryRef(
                    name=name,
                    full_name=full_name,
                    default_branch=default_branch,
                    archived=item.get("archived") is True,
                )
            )
        return sorted(repositories, key=lambda repository: repository.name.casefold())

    async def sync_repository(
        self,
        repository: RepositoryRef,
        cursors: dict[str, str | int | None],
        *,
        include_files: bool = True,
        activity_since: datetime | None = None,
        request_budget: _ActivityRequestBudget | None = None,
    ) -> GitHubBatch:
        if activity_since is not None and (
            activity_since.tzinfo is None
            or activity_since.utcoffset() != UTC.utcoffset(activity_since)
        ):
            raise ValueError("GitHub activity cutoff must use UTC")
        page_budget = _MAX_ACTIVITY_PAGES if activity_since is not None else None
        records: list[SourceRecord] = []
        updated_cursors = dict(cursors)
        if include_files:
            branch = self._require_mapping(
                await self.client.get_json(
                    f"/repos/{repository.full_name}/branches/{quote(repository.default_branch)}"
                )
            )
            commit = self._require_mapping(branch.get("commit"))
            branch_sha = self._require_string(commit.get("sha"), "default branch SHA")
            branch_time = self._branch_timestamp(commit)
            if cursors.get("default_branch_sha") != branch_sha:
                records.extend(await self._collect_files(repository, branch_sha, branch_time))
                updated_cursors["default_branch_sha"] = branch_sha

        commits = await self._paginate(
            f"/repos/{repository.full_name}/commits",
            self._since(cursors, "commits_since", {"sha": repository.default_branch}),
            max_pages=page_budget,
            request_budget=request_budget,
        )
        commit_records = self._map_commits(repository, commits)
        records.extend(commit_records)
        self._advance_timestamp(
            updated_cursors, "commits_since", commit_records, SourceType.GITHUB_COMMIT
        )

        issues = await self._paginate(
            f"/repos/{repository.full_name}/issues",
            self._since(cursors, "issues_since", {"state": "all", "sort": "updated"}),
            max_pages=page_budget,
            request_budget=request_budget,
        )
        issue_records = self._map_issues(repository, issues)
        records.extend(issue_records)
        self._advance_timestamp(
            updated_cursors,
            "issues_since",
            issue_records,
            SourceType.GITHUB_ISSUE,
            SourceType.GITHUB_PULL_REQUEST,
        )

        issue_comments = await self._paginate(
            f"/repos/{repository.full_name}/issues/comments",
            self._since(cursors, "issue_comments_since", {"sort": "updated"}),
            max_pages=page_budget,
            request_budget=request_budget,
        )
        issue_comment_records = self._map_issue_comments(repository, issue_comments)
        records.extend(issue_comment_records)
        self._advance_timestamp(
            updated_cursors,
            "issue_comments_since",
            issue_comment_records,
            SourceType.GITHUB_COMMENT,
        )

        if activity_since is None:
            pulls = await self._paginate(
                f"/repos/{repository.full_name}/pulls",
                {"state": "all", "sort": "updated", "direction": "asc"},
                max_pages=page_budget,
                request_budget=request_budget,
            )
        else:
            pulls = await self._paginate_recent(
                f"/repos/{repository.full_name}/pulls",
                {"state": "all", "sort": "updated", "direction": "desc"},
                timestamp_key="updated_at",
                since=activity_since,
                max_pages=_MAX_ACTIVITY_PAGES,
                request_budget=request_budget,
            )
        review_pulls = self._items_after(pulls, cursors.get("pulls_since"), "updated_at")
        review_records = await self._collect_reviews(
            repository,
            review_pulls,
            activity_since=activity_since,
            max_pages=page_budget,
            request_budget=request_budget,
        )
        records.extend(review_records)
        self._advance_timestamp(
            updated_cursors, "reviews_since", review_records, SourceType.GITHUB_REVIEW
        )
        pull_updates = [item.get("updated_at") for item in pulls]
        self._advance_values(updated_cursors, "pulls_since", pull_updates)

        review_comments = await self._paginate(
            f"/repos/{repository.full_name}/pulls/comments",
            self._since(cursors, "review_comments_since", {"sort": "updated"}),
            max_pages=page_budget,
            request_budget=request_budget,
        )
        review_comment_records = self._map_review_comments(repository, review_comments)
        records.extend(review_comment_records)
        self._advance_timestamp(
            updated_cursors,
            "review_comments_since",
            review_comment_records,
            SourceType.GITHUB_REVIEW,
        )

        if activity_since is None:
            release_items = await self._paginate(
                f"/repos/{repository.full_name}/releases",
                {},
                max_pages=page_budget,
                request_budget=request_budget,
            )
        else:
            release_items = await self._paginate(
                f"/repos/{repository.full_name}/releases",
                {},
                max_pages=page_budget,
                request_budget=request_budget,
            )
        release_items = self._items_after(
            release_items, cursors.get("releases_since"), "published_at"
        )
        release_records = self._map_releases(repository, release_items)
        records.extend(release_records)
        self._advance_timestamp(
            updated_cursors, "releases_since", release_records, SourceType.GITHUB_RELEASE
        )

        discussion_records, discussion_cursor = await self._collect_discussions(
            repository,
            cursors.get("discussions_cursor"),
            activity_since=activity_since,
            max_pages=page_budget,
            request_budget=request_budget,
        )
        records.extend(discussion_records)
        if discussion_cursor is not None:
            updated_cursors["discussions_cursor"] = discussion_cursor

        by_id = {record.record_id: record for record in records}
        return GitHubBatch(
            records=tuple(by_id[record_id] for record_id in sorted(by_id)),
            cursors=updated_cursors,
        )

    async def sync_activity_since(
        self, since: datetime, *, max_concurrency: int = 4
    ) -> GitHubBatch:
        if since.tzinfo is None or since.utcoffset() != UTC.utcoffset(since):
            raise ValueError("GitHub activity cutoff must use UTC")
        if max_concurrency <= 0:
            raise ValueError("GitHub activity concurrency must be positive")
        request_budget = _ActivityRequestBudget(_MAX_ACTIVITY_REQUESTS)
        repositories = await self.list_public_repositories(request_budget=request_budget)
        semaphore = asyncio.Semaphore(max_concurrency)
        baseline = (since - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        cursor_keys = (
            "commits_since",
            "issues_since",
            "issue_comments_since",
            "pulls_since",
            "reviews_since",
            "review_comments_since",
            "releases_since",
        )

        async def collect(repository: RepositoryRef) -> tuple[RepositoryRef, GitHubBatch | None]:
            cursors: dict[str, str | int | None] = {key: baseline for key in cursor_keys}
            try:
                async with semaphore:
                    batch = await self.sync_repository(
                        repository,
                        cursors,
                        include_files=False,
                        activity_since=since,
                        request_budget=request_budget,
                    )
            except GitHubSourceError:
                return repository, None
            return repository, batch

        results = await asyncio.gather(*(collect(repository) for repository in repositories))
        records: list[SourceRecord] = []
        cursors: dict[str, str | int | None] = {}
        failures: list[str] = []
        for repository, batch in results:
            if batch is None:
                failures.append(repository.name)
                continue
            records.extend(record for record in batch.records if record.updated_at >= since)
            cursors.update(
                {f"{repository.name}:{key}": value for key, value in batch.cursors.items()}
            )
        by_id = {record.record_id: record for record in records}
        return GitHubBatch(
            records=tuple(by_id[key] for key in sorted(by_id)),
            cursors=cursors,
            failed_streams=tuple(failures),
        )

    async def sync_all(self, cursors: SourceCursors) -> GitHubBatch:
        records: list[SourceRecord] = []
        next_cursors = dict(cursors.github)
        failures: list[str] = []
        for repository in await self.list_public_repositories():
            prefix = f"{repository.name}:"
            repository_cursors = {
                key.removeprefix(prefix): value
                for key, value in cursors.github.items()
                if key.startswith(prefix)
            }
            try:
                batch = await self.sync_repository(repository, repository_cursors)
            except GitHubSourceError:
                failures.append(repository.name)
                continue
            records.extend(batch.records)
            for key, value in batch.cursors.items():
                next_cursors[f"{prefix}{key}"] = value
        by_id = {record.record_id: record for record in records}
        return GitHubBatch(
            records=tuple(by_id[record_id] for record_id in sorted(by_id)),
            cursors=next_cursors,
            failed_streams=tuple(failures),
        )

    async def _collect_files(
        self, repository: RepositoryRef, branch_sha: str, branch_time: datetime
    ) -> list[SourceRecord]:
        tree_payload = self._require_mapping(
            await self.client.get_json(
                f"/repos/{repository.full_name}/git/trees/{branch_sha}", {"recursive": "1"}
            )
        )
        if tree_payload.get("truncated") is True:
            items = await self._walk_contents(repository, branch_sha)
        else:
            raw_items = tree_payload.get("tree")
            if not isinstance(raw_items, list):
                raise GitHubSourceError("GitHub tree response is invalid")
            items = [self._require_mapping(item) for item in raw_items]
        records: list[SourceRecord] = []
        for item in items:
            if item.get("type") != "blob":
                continue
            path = self._require_string(item.get("path"), "tree path")
            sha = self._require_string(item.get("sha"), "blob SHA")
            size = item.get("size") if isinstance(item.get("size"), int) else None
            decision = self.path_policy.classify(path, size)
            if not decision.include_record:
                continue
            if decision.include_body:
                blob_text = await self._blob_text(repository, sha)
                if blob_text is None:
                    continue
                text = blob_text
            else:
                text = ""
            record = self._make_record(
                record_id=github_id(
                    repository.name,
                    "file",
                    hashlib.sha256(path.encode()).hexdigest(),
                ),
                source_type=SourceType.GITHUB_FILE,
                locator=(
                    f"https://github.com/{repository.full_name}/blob/{branch_sha}/{quote(path)}"
                ),
                author=None,
                created_at=branch_time,
                updated_at=branch_time,
                text=text,
                payload={
                    "path": path,
                    "ref": repository.default_branch,
                    "commit_sha": branch_sha,
                    "body_included": decision.include_body,
                    "filter_reason": decision.reason,
                },
            )
            if record is not None:
                records.append(record)
        return records

    async def _walk_contents(
        self, repository: RepositoryRef, branch_sha: str
    ) -> list[dict[str, Any]]:
        pending = [""]
        files: list[dict[str, Any]] = []
        while pending:
            path = pending.pop()
            suffix = f"/{quote(path)}" if path else ""
            payload = await self.client.get_json(
                f"/repos/{repository.full_name}/contents{suffix}", {"ref": branch_sha}
            )
            if not isinstance(payload, list):
                raise GitHubSourceError("GitHub contents traversal returned an invalid page")
            for raw_item in payload:
                item = self._require_mapping(raw_item)
                item_type = item.get("type")
                item_path = self._require_string(item.get("path"), "contents path")
                if item_type == "dir":
                    if len(_path_parts(item_path)) <= 32:
                        pending.append(item_path)
                elif item_type == "file":
                    files.append(
                        {
                            "type": "blob",
                            "path": item_path,
                            "sha": item.get("sha"),
                            "size": item.get("size"),
                        }
                    )
                if len(files) + len(pending) > 20_000:
                    raise GitHubSourceError("GitHub contents traversal exceeded its bound")
        return files

    async def _blob_text(self, repository: RepositoryRef, sha: str) -> str | None:
        payload = self._require_mapping(
            await self.client.get_json(f"/repos/{repository.full_name}/git/blobs/{sha}")
        )
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise GitHubSourceError("GitHub blob response is invalid")
        try:
            decoded = base64.b64decode(payload["content"], validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        return None if "\x00" in decoded else decoded

    def _map_commits(
        self, repository: RepositoryRef, items: list[dict[str, Any]]
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for item in items:
            sha = self._require_string(item.get("sha"), "commit SHA")
            commit = self._require_mapping(item.get("commit"))
            author_data = self._require_mapping(commit.get("author"))
            committer_data = self._require_mapping(commit.get("committer"))
            created = self._timestamp(author_data.get("date"))
            updated = self._timestamp(committer_data.get("date"))
            account = item.get("author")
            login = account.get("login") if isinstance(account, dict) else author_data.get("name")
            record = self._make_record(
                record_id=github_id(repository.name, "commit", sha),
                source_type=SourceType.GITHUB_COMMIT,
                locator=self._require_string(item.get("html_url"), "commit URL"),
                author=login if isinstance(login, str) else None,
                created_at=created,
                updated_at=max(created, updated),
                text=str(commit.get("message", "")),
                payload={"sha": sha},
            )
            if record is not None:
                records.append(record)
        return records

    def _map_issues(
        self, repository: RepositoryRef, items: list[dict[str, Any]]
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for item in items:
            number = self._require_int(item.get("number"), "issue number")
            is_pull = "pull_request" in item
            kind = "pr" if is_pull else "issue"
            source_type = SourceType.GITHUB_PULL_REQUEST if is_pull else SourceType.GITHUB_ISSUE
            record = self._make_record(
                record_id=github_id(repository.name, kind, number),
                source_type=source_type,
                locator=self._require_string(item.get("html_url"), "issue URL"),
                author=self._login(item.get("user")),
                created_at=self._timestamp(item.get("created_at")),
                updated_at=self._timestamp(item.get("updated_at")),
                text=self._title_body(item),
                payload={"number": number},
            )
            if record is not None:
                records.append(record)
        return records

    def _map_issue_comments(
        self, repository: RepositoryRef, items: list[dict[str, Any]]
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for item in items:
            comment_id = self._require_int(item.get("id"), "issue comment ID")
            issue_number = self._number_from_url(item.get("issue_url"))
            record = self._make_record(
                record_id=github_id(repository.name, "issue", issue_number, "comment", comment_id),
                source_type=SourceType.GITHUB_COMMENT,
                locator=self._require_string(item.get("html_url"), "issue comment URL"),
                author=self._login(item.get("user")),
                created_at=self._timestamp(item.get("created_at")),
                updated_at=self._timestamp(item.get("updated_at")),
                text=str(item.get("body", "")),
                payload={"issue_number": issue_number, "comment_kind": "issue"},
                relations=[
                    Relation(
                        relation_type="comment_on",
                        target_record_id=github_id(repository.name, "issue", issue_number),
                    )
                ],
            )
            if record is not None:
                records.append(record)
        return records

    async def _collect_reviews(
        self,
        repository: RepositoryRef,
        pulls: list[dict[str, Any]],
        *,
        activity_since: datetime | None = None,
        max_pages: int | None = None,
        request_budget: _ActivityRequestBudget | None = None,
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for pull in pulls:
            number = self._require_int(pull.get("number"), "pull request number")
            reviews = await self._paginate(
                f"/repos/{repository.full_name}/pulls/{number}/reviews",
                {},
                max_pages=max_pages,
                request_budget=request_budget,
            )
            for review in reviews:
                review_id = self._require_int(review.get("id"), "review ID")
                submitted = self._timestamp(review.get("submitted_at"))
                record = self._make_record(
                    record_id=github_id(repository.name, "pr", number, "review", review_id),
                    source_type=SourceType.GITHUB_REVIEW,
                    locator=self._require_string(review.get("html_url"), "review URL"),
                    author=self._login(review.get("user")),
                    created_at=submitted,
                    updated_at=submitted,
                    text=str(review.get("body", "")),
                    payload={"pull_number": number, "state": review.get("state")},
                    relations=[
                        Relation(
                            relation_type="review_of",
                            target_record_id=github_id(repository.name, "pr", number),
                        )
                    ],
                )
                if record is not None:
                    if activity_since is not None and record.updated_at < activity_since:
                        continue
                    records.append(record)
        return records

    def _map_review_comments(
        self, repository: RepositoryRef, items: list[dict[str, Any]]
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for item in items:
            comment_id = self._require_int(item.get("id"), "review comment ID")
            pull_number = self._number_from_url(item.get("pull_request_url"))
            record = self._make_record(
                record_id=github_id(
                    repository.name, "pr", pull_number, "review-comment", comment_id
                ),
                source_type=SourceType.GITHUB_REVIEW,
                locator=self._require_string(item.get("html_url"), "review comment URL"),
                author=self._login(item.get("user")),
                created_at=self._timestamp(item.get("created_at")),
                updated_at=self._timestamp(item.get("updated_at")),
                text=str(item.get("body", "")),
                payload={"pull_number": pull_number, "review_kind": "comment"},
                relations=[
                    Relation(
                        relation_type="review_of",
                        target_record_id=github_id(repository.name, "pr", pull_number),
                    )
                ],
            )
            if record is not None:
                records.append(record)
        return records

    def _map_releases(
        self, repository: RepositoryRef, items: list[dict[str, Any]]
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for item in items:
            release_id = self._require_int(item.get("id"), "release ID")
            created = self._timestamp(item.get("created_at"))
            published = self._timestamp(item.get("published_at") or item.get("created_at"))
            record = self._make_record(
                record_id=github_id(repository.name, "release", release_id),
                source_type=SourceType.GITHUB_RELEASE,
                locator=self._require_string(item.get("html_url"), "release URL"),
                author=self._login(item.get("author")),
                created_at=created,
                updated_at=max(created, published),
                text=self._release_text(item),
                payload={"tag_name": item.get("tag_name")},
            )
            if record is not None:
                records.append(record)
        return records

    async def _collect_discussions(
        self,
        repository: RepositoryRef,
        cursor: str | int | None,
        *,
        activity_since: datetime | None = None,
        max_pages: int | None = None,
        request_budget: _ActivityRequestBudget | None = None,
    ) -> tuple[list[SourceRecord], str | None]:
        records: list[SourceRecord] = []
        # Relay cursors are traversal positions, not durable update cursors. Revisit the
        # connection so edits and new comments on older discussions cannot be missed.
        after = None
        final_cursor = cursor if isinstance(cursor, str) else None
        page_count = 0
        while True:
            page_count += 1
            if max_pages is not None and page_count > max_pages:
                raise GitHubSourceError("GitHub discussion pagination exceeded its budget")
            self._consume_request(request_budget)
            data = await self.client.graphql(
                _RECENT_DISCUSSIONS_QUERY if activity_since is not None else _DISCUSSIONS_QUERY,
                {"owner": self.organization, "name": repository.name, "after": after},
            )
            repository_data = self._require_mapping(data.get("repository"))
            discussions = self._require_mapping(repository_data.get("discussions"))
            nodes = discussions.get("nodes")
            page_info = self._require_mapping(discussions.get("pageInfo"))
            if not isinstance(nodes, list):
                raise GitHubSourceError("GitHub discussions response is invalid")
            for raw_discussion in nodes:
                discussion = self._require_mapping(raw_discussion)
                number = self._require_int(discussion.get("number"), "discussion number")
                record = self._make_record(
                    record_id=github_id(repository.name, "discussion", number),
                    source_type=SourceType.GITHUB_DISCUSSION,
                    locator=self._require_string(discussion.get("url"), "discussion URL"),
                    author=self._login(discussion.get("author")),
                    created_at=self._timestamp(discussion.get("createdAt")),
                    updated_at=self._timestamp(discussion.get("updatedAt")),
                    text=self._title_body(discussion),
                    payload={"number": number},
                )
                if record is not None:
                    records.append(record)
                comments_data = self._require_mapping(discussion.get("comments"))
                comments = comments_data.get("nodes", [])
                if not isinstance(comments, list):
                    raise GitHubSourceError("GitHub discussion comments are invalid")
                comment_records = self._map_discussion_comments(repository, number, comments)
                records.extend(
                    record
                    for record in comment_records
                    if activity_since is None or record.updated_at >= activity_since
                )
                comments_page = self._require_mapping(comments_data.get("pageInfo"))
                page_direction = "before" if activity_since is not None else "after"
                page_flag = "hasPreviousPage" if activity_since is not None else "hasNextPage"
                cursor_key = "startCursor" if activity_since is not None else "endCursor"
                comment_cursor = comments_page.get(cursor_key)
                comment_page_count = 1
                while comments_page.get(page_flag) is True:
                    comment_page_count += 1
                    if max_pages is not None and comment_page_count > max_pages:
                        raise GitHubSourceError(
                            "GitHub discussion comment pagination exceeded its budget"
                        )
                    if not isinstance(comment_cursor, str):
                        raise GitHubSourceError(
                            "GitHub discussion comment pagination did not advance"
                        )
                    self._consume_request(request_budget)
                    comment_data = await self.client.graphql(
                        _RECENT_DISCUSSION_COMMENTS_QUERY
                        if activity_since is not None
                        else _DISCUSSION_COMMENTS_QUERY,
                        {
                            "owner": self.organization,
                            "name": repository.name,
                            "number": number,
                            page_direction: comment_cursor,
                        },
                    )
                    comment_repository = self._require_mapping(comment_data.get("repository"))
                    discussion_page = self._require_mapping(comment_repository.get("discussion"))
                    comments_data = self._require_mapping(discussion_page.get("comments"))
                    page_nodes = comments_data.get("nodes")
                    if not isinstance(page_nodes, list):
                        raise GitHubSourceError("GitHub discussion comments are invalid")
                    page_records = self._map_discussion_comments(repository, number, page_nodes)
                    records.extend(
                        record
                        for record in page_records
                        if activity_since is None or record.updated_at >= activity_since
                    )
                    comments_page = self._require_mapping(comments_data.get("pageInfo"))
                    next_comment_cursor = comments_page.get(cursor_key)
                    if (
                        comments_page.get(page_flag) is True
                        and next_comment_cursor == comment_cursor
                    ):
                        raise GitHubSourceError(
                            "GitHub discussion comment pagination did not advance"
                        )
                    comment_cursor = next_comment_cursor
            end_cursor = page_info.get("endCursor")
            final_cursor = end_cursor if isinstance(end_cursor, str) else final_cursor
            if page_info.get("hasNextPage") is not True:
                return records, final_cursor
            if not isinstance(end_cursor, str) or end_cursor == after:
                raise GitHubSourceError("GitHub discussion pagination did not advance")
            after = end_cursor

    def _map_discussion_comments(
        self,
        repository: RepositoryRef,
        discussion_number: int,
        comments: list[object],
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for raw_comment in comments:
            comment = self._require_mapping(raw_comment)
            comment_id = self._require_string(comment.get("id"), "discussion comment ID")
            record = self._make_record(
                record_id=github_id(
                    repository.name,
                    "discussion",
                    discussion_number,
                    "comment",
                    comment_id,
                ),
                source_type=SourceType.GITHUB_COMMENT,
                locator=self._require_string(comment.get("url"), "discussion comment URL"),
                author=self._login(comment.get("author")),
                created_at=self._timestamp(comment.get("createdAt")),
                updated_at=self._timestamp(comment.get("updatedAt")),
                text=str(comment.get("body", "")),
                payload={
                    "discussion_number": discussion_number,
                    "comment_kind": "discussion",
                },
                relations=[
                    Relation(
                        relation_type="comment_on",
                        target_record_id=github_id(
                            repository.name, "discussion", discussion_number
                        ),
                    )
                ],
            )
            if record is not None:
                records.append(record)
        return records

    async def _paginate(
        self,
        path: str,
        params: dict[str, object],
        *,
        max_pages: int | None = None,
        request_budget: _ActivityRequestBudget | None = None,
    ) -> list[dict[str, Any]]:
        if max_pages is not None and max_pages <= 0:
            raise ValueError("GitHub pagination budget must be positive")
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            if max_pages is not None and page > max_pages:
                raise GitHubSourceError("GitHub pagination exceeded its budget")
            query = {**params, "per_page": 100, "page": page}
            self._consume_request(request_budget)
            payload = await self.client.get_json(path, query)
            if not isinstance(payload, list):
                raise GitHubSourceError("GitHub paginated response was not a list")
            if not payload:
                return records
            for item in payload:
                records.append(self._require_mapping(item))
            if len(payload) < 100:
                return records
            page += 1

    async def _paginate_recent(
        self,
        path: str,
        params: dict[str, object],
        *,
        timestamp_key: str,
        since: datetime,
        max_pages: int,
        request_budget: _ActivityRequestBudget | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            query = {**params, "per_page": 100, "page": page}
            self._consume_request(request_budget)
            payload = await self.client.get_json(path, query)
            if not isinstance(payload, list):
                raise GitHubSourceError("GitHub paginated response was not a list")
            if not payload:
                return records
            crossed_boundary = False
            for raw_item in payload:
                item = self._require_mapping(raw_item)
                timestamp = self._timestamp(item.get(timestamp_key))
                if timestamp < since:
                    crossed_boundary = True
                    continue
                records.append(item)
            if crossed_boundary:
                return records
            if len(payload) < 100:
                return records
        raise GitHubSourceError("GitHub activity pagination exceeded its budget")

    @staticmethod
    def _consume_request(budget: _ActivityRequestBudget | None) -> None:
        if budget is not None:
            budget.consume()

    def _make_record(
        self,
        *,
        record_id: str,
        source_type: SourceType,
        locator: str,
        author: str | None,
        created_at: datetime,
        updated_at: datetime,
        text: str,
        payload: dict[str, Any],
        relations: list[Relation] | None = None,
    ) -> SourceRecord | None:
        inspected = self.privacy.inspect(text)
        if not inspected.accepted or inspected.sanitized_text is None:
            return None
        text = inspected.sanitized_text
        safe_author = self._safe_author(author)
        person_id = safe_author.casefold() if safe_author else None
        return SourceRecord.from_text(
            record_id=record_id,
            source_type=source_type,
            source_locator=locator,
            author_person_id=person_id,
            author_source_handle=safe_author,
            created_at=created_at,
            updated_at=updated_at,
            text_original=text,
            relations=relations,
            ingested_at=self.now(),
            source_payload=payload,
        )

    def _safe_author(self, author: str | None) -> str | None:
        if author is None:
            return None
        inspected = self.privacy.inspect(author)
        if not inspected.accepted or not inspected.sanitized_text:
            return None
        return inspected.sanitized_text

    @staticmethod
    def _since(
        cursors: dict[str, str | int | None], key: str, base: dict[str, object]
    ) -> dict[str, object]:
        result = dict(base)
        value = cursors.get(key)
        if isinstance(value, str):
            result["since"] = value
        return result

    @staticmethod
    def _advance_timestamp(
        cursors: dict[str, str | int | None],
        key: str,
        records: list[SourceRecord],
        *source_types: SourceType,
    ) -> None:
        values = [
            record.updated_at.isoformat().replace("+00:00", "Z")
            for record in records
            if record.source_type in source_types
        ]
        GitHubSource._advance_values(cursors, key, values)

    @staticmethod
    def _advance_values(
        cursors: dict[str, str | int | None], key: str, values: Sequence[object]
    ) -> None:
        strings = [value for value in values if isinstance(value, str)]
        current = cursors.get(key)
        if isinstance(current, str):
            strings.append(current)
        if strings:
            cursors[key] = max(strings)

    @staticmethod
    def _items_after(
        items: list[dict[str, Any]], cursor: str | int | None, timestamp_key: str
    ) -> list[dict[str, Any]]:
        if not isinstance(cursor, str):
            return items
        return [
            item
            for item in items
            if isinstance(item.get(timestamp_key), str) and item[timestamp_key] > cursor
        ]

    @staticmethod
    def _branch_timestamp(commit: dict[str, Any]) -> datetime:
        commit_data = commit.get("commit")
        if isinstance(commit_data, dict):
            committer = commit_data.get("committer")
            if isinstance(committer, dict):
                return GitHubSource._timestamp(committer.get("date"))
        return datetime.fromtimestamp(0, tz=UTC)

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise GitHubSourceError("GitHub timestamp is missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise GitHubSourceError("GitHub timestamp has no timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _title_body(item: dict[str, Any]) -> str:
        title = str(item.get("title", "")).strip()
        body = str(item.get("body") or "").strip()
        return f"{title}\n\n{body}".strip()

    @staticmethod
    def _release_text(item: dict[str, Any]) -> str:
        heading = str(item.get("name") or item.get("tag_name") or "").strip()
        body = str(item.get("body") or "").strip()
        return f"{heading}\n\n{body}".strip()

    @staticmethod
    def _login(value: object) -> str | None:
        if isinstance(value, dict):
            login = value.get("login")
            if isinstance(login, str):
                return login
        return None

    @staticmethod
    def _number_from_url(value: object) -> int:
        url = GitHubSource._require_string(value, "GitHub relation URL")
        try:
            return int(url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            raise GitHubSourceError("GitHub relation URL has no numeric identifier") from None

    @staticmethod
    def _require_mapping(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise GitHubSourceError("GitHub response object is invalid")
        return value

    @staticmethod
    def _require_string(value: object, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubSourceError(f"{label} is missing")
        return value

    @staticmethod
    def _require_int(value: object, label: str) -> int:
        if not isinstance(value, int):
            raise GitHubSourceError(f"{label} is missing")
        return value


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)
