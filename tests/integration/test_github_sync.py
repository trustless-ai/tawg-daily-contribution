import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.github_source import GitHubSource, GitHubSourceError
from tawg_bot.models import SourceCursors, SourceType

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 30, tzinfo=UTC)


class FakeGitHubClient:
    def __init__(self) -> None:
        self.repositories = json.loads(
            (ROOT / "tests/fixtures/github/repositories.json").read_text()
        )
        self.snapshot = json.loads((ROOT / "tests/fixtures/github/snapshot.json").read_text())
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> list[Any] | dict[str, Any]:
        query = params or {}
        self.calls.append((path, query))
        page = int(query.get("page", 1))
        if path == "/orgs/trustless-ai/repos":
            return self.repositories if page == 1 else []
        if path.endswith("/branches/main"):
            return self.snapshot["branch"]
        if "/git/trees/" in path:
            return self.snapshot["tree"]
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[-1]
            content = base64.b64encode(self.snapshot["blobs"][sha].encode()).decode()
            return {"encoding": "base64", "content": content}
        route = path.rsplit("/", 1)[-1]
        mapping = {
            "commits": "commits",
            "issues": "issues",
            "comments": "issue_comments",
            "pulls": "pulls",
            "releases": "releases",
        }
        if "/pulls/8/reviews" in path:
            return self.snapshot["reviews"] if page == 1 else []
        if path.endswith("/pulls/comments"):
            return self.snapshot["review_comments"] if page == 1 else []
        if route in mapping:
            return self.snapshot[mapping[route]] if page == 1 else []
        raise AssertionError(f"unexpected GitHub route: {path} {query}")

    async def graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("graphql", variables))
        return {
            "repository": {
                "discussions": {
                    "nodes": self.snapshot["discussions"],
                    "pageInfo": {"hasNextPage": False, "endCursor": "discussion-end"},
                }
            }
        }


@pytest.mark.asyncio
async def test_sync_discovers_all_public_repositories_including_archived_and_future() -> None:
    client = FakeGitHubClient()
    source = GitHubSource.for_repository(root=ROOT, client=client, now=lambda: NOW)

    repositories = await source.list_public_repositories()

    assert [repo.name for repo in repositories] == [
        "agent-ercs",
        "archived-notes",
        "future-repo",
    ]
    assert repositories[1].archived


@pytest.mark.asyncio
async def test_repository_sync_covers_source_and_collaboration_evidence() -> None:
    client = FakeGitHubClient()
    source = GitHubSource.for_repository(root=ROOT, client=client, now=lambda: NOW)
    repository = (await source.list_public_repositories())[0]

    batch = await source.sync_repository(repository, {})

    types = {record.source_type for record in batch.records}
    assert types == {
        SourceType.GITHUB_FILE,
        SourceType.GITHUB_COMMIT,
        SourceType.GITHUB_ISSUE,
        SourceType.GITHUB_PULL_REQUEST,
        SourceType.GITHUB_COMMENT,
        SourceType.GITHUB_REVIEW,
        SourceType.GITHUB_DISCUSSION,
        SourceType.GITHUB_RELEASE,
    }
    paths = {record.source_payload.get("path") for record in batch.records}
    assert "README.md" in paths
    assert "src/agent.py" in paths
    lock = next(
        record
        for record in batch.records
        if record.source_payload.get("path") == "package-lock.json"
    )
    assert lock.text_original == ""
    serialized = json.dumps([record.model_dump(mode="json") for record in batch.records])
    assert "this body must never be retained" not in serialized
    assert "node_modules" not in serialized
    assert "dist/generated.js" not in serialized
    assert "logo.png" not in serialized
    assert batch.cursors["default_branch_sha"] == "abc123"
    assert batch.cursors["discussions_cursor"] == "discussion-end"


@pytest.mark.asyncio
async def test_truncated_tree_falls_back_to_bounded_contents_walk() -> None:
    class TruncatedTreeClient(FakeGitHubClient):
        async def get_json(
            self, path: str, params: dict[str, object] | None = None
        ) -> list[Any] | dict[str, Any]:
            if "/git/trees/" in path:
                return {"truncated": True, "tree": []}
            if path.endswith("/contents"):
                return [
                    {"type": "dir", "path": "docs", "sha": "tree-docs"},
                    {"type": "file", "path": "README.md", "sha": "blob-readme", "size": 90},
                ]
            if path.endswith("/contents/docs"):
                return [
                    {"type": "file", "path": "docs/spec.md", "sha": "blob-source", "size": 70}
                ]
            return await super().get_json(path, params)

    client = TruncatedTreeClient()
    source = GitHubSource.for_repository(root=ROOT, client=client, now=lambda: NOW)
    repository = (await source.list_public_repositories())[0]

    batch = await source.sync_repository(repository, {})

    paths = {record.source_payload.get("path") for record in batch.records}
    assert {"README.md", "docs/spec.md"}.issubset(paths)


@pytest.mark.asyncio
async def test_failed_repository_keeps_old_stream_cursors_and_marks_batch_failed() -> None:
    class PartiallyFailingClient(FakeGitHubClient):
        async def get_json(
            self, path: str, params: dict[str, object] | None = None
        ) -> list[Any] | dict[str, Any]:
            if "/repos/trustless-ai/archived-notes/" in path:
                raise GitHubSourceError("injected archived repository failure")
            if "/repos/trustless-ai/future-repo/" in path:
                raise GitHubSourceError("injected future repository failure")
            return await super().get_json(path, params)

    old = "2026-08-01T00:00:00Z"
    cursors = SourceCursors(github={"archived-notes:commits_since": old})
    source = GitHubSource.for_repository(
        root=ROOT, client=PartiallyFailingClient(), now=lambda: NOW
    )

    batch = await source.sync_all(cursors)

    assert batch.failed_streams == ("archived-notes", "future-repo")
    assert batch.cursors["archived-notes:commits_since"] == old
    assert any(record.record_id.startswith("gh:agent-ercs:") for record in batch.records)


@pytest.mark.asyncio
async def test_discussion_comments_follow_graphql_cursors_until_complete() -> None:
    class PaginatedCommentClient(FakeGitHubClient):
        def __init__(self) -> None:
            super().__init__()
            page_info = self.snapshot["discussions"][0]["comments"]["pageInfo"]
            page_info.update({"hasNextPage": True, "endCursor": "comment-page-1"})

        async def graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if variables.get("number") == 4:
                assert variables["after"] == "comment-page-1"
                return {
                    "repository": {
                        "discussion": {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "DC_kwDO3",
                                        "body": "Second page evidence.",
                                        "url": "https://github.com/trustless-ai/agent-ercs/discussions/4#discussioncomment-2",
                                        "createdAt": "2026-08-22T16:20:00Z",
                                        "updatedAt": "2026-08-22T16:20:00Z",
                                        "author": {"login": "grace"},
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": "comment-page-2",
                                },
                            }
                        }
                    }
                }
            return await super().graphql(query, variables)

    client = PaginatedCommentClient()
    source = GitHubSource.for_repository(root=ROOT, client=client, now=lambda: NOW)
    repository = (await source.list_public_repositories())[0]

    batch = await source.sync_repository(repository, {})

    assert any(record.text_original == "Second page evidence." for record in batch.records)
