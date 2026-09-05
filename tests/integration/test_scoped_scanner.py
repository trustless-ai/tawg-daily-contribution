from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tawg_bot.models import SourceCursors
from tawg_bot.scoped_scanner import (
    RepositoryDescriptor,
    ScopedObservation,
    ScopedScanRejected,
    ScopedScanResult,
    ScopedSourceScanner,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork

NOW = datetime(2026, 8, 28, 3, tzinfo=UTC)
MAGICIANS = "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902"
PROPOSAL_PR = "https://github.com/ethereum/ERCs/pull/1081"


class GitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def get_json(self, path: str, params=None) -> list[Any] | dict[str, Any]:
        self.calls.append((path, params))
        if path == "/orgs/trustless-ai/repos":
            if params["page"] > 1:
                return []
            return [
                {
                    "id": 1,
                    "name": "active",
                    "full_name": "trustless-ai/active",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                    "updated_at": "2026-08-28T02:00:00Z",
                },
                {
                    "id": 2,
                    "name": "archive",
                    "full_name": "trustless-ai/archive",
                    "default_branch": "main",
                    "private": False,
                    "archived": True,
                    "updated_at": "2026-08-27T02:00:00Z",
                },
                {
                    "id": 3,
                    "name": "private",
                    "full_name": "trustless-ai/private",
                    "default_branch": "main",
                    "private": True,
                    "archived": False,
                    "updated_at": "2026-08-28T02:00:00Z",
                },
            ]
        if path == "/repos/ethereum/ERCs/pulls/1081":
            return {
                "id": 1081,
                "number": 1081,
                "state": "open",
                "title": "Add ERC-8183",
                "updated_at": "2026-08-28T02:30:00Z",
            }
        if path in {
            "/repos/ethereum/ERCs/issues/1081/comments",
            "/repos/ethereum/ERCs/pulls/1081/reviews",
            "/repos/ethereum/ERCs/pulls/1081/comments",
        }:
            if params["page"] > 1:
                return []
            return [
                {
                    "id": 7,
                    "updated_at": "2026-08-28T02:45:00Z",
                    "user": {"login": "reviewer"},
                    "body": "This body must never be persisted.",
                }
            ]
        raise AssertionError(path)


class TopicClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def get_json(self, path: str, params=None) -> dict[str, Any]:
        del params
        self.paths.append(path)
        assert path == "/t/27902.json"
        return {
            "id": 27902,
            "slug": "erc-8183-agentic-commerce",
            "title": "ERC-8183 Agentic Commerce",
            "posts_count": 17,
            "highest_post_number": 17,
            "last_posted_at": "2026-08-28T02:40:00Z",
            "post_stream": {"posts": [{"cooked": "must not persist"}]},
        }


def _scaffold(root: Path) -> None:
    scan_targets = root / "knowledge/meta/scan-targets.yml"
    scan_targets.parent.mkdir(parents=True, exist_ok=True)
    scan_targets.write_text(
        "schema: tawg.scan-targets.v1\n"
        "github_organization: trustless-ai\n"
        "include_public_archived_repositories: true\n"
        "ercs:\n"
        "- erc_number: 8183\n"
        f"  magicians_topic_url: {MAGICIANS}\n"
        f"  proposal_pr_url: {PROPOSAL_PR}\n"
        "  registered_from_record_id: tg:tawg:6000\n"
        "  registered_at: '2026-08-28T00:00:00Z'\n",
        encoding="utf-8",
    )
    state = root / "data/state/source-cursors.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(SourceCursors().model_dump_json(indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_scan_uses_all_org_repos_and_only_registered_erc_sources(
    tmp_path: Path,
) -> None:
    _scaffold(tmp_path)
    github = GitHubClient()
    topics = TopicClient()
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=github,
        topic_client=topics,
    )

    result = await scanner.scan(since=NOW - timedelta(minutes=30), now=NOW)

    assert {
        observation.source_locator for observation in result.observations
    } == {
        "https://github.com/trustless-ai/active",
        "https://github.com/trustless-ai/archive",
        MAGICIANS,
        PROPOSAL_PR,
    }
    assert result.failed_sources == ()
    assert topics.paths == ["/t/27902.json"]
    assert not any(
        "/repos/ethereum/ERCs/pulls/" in path and "1081" not in path
        for path, _ in github.calls
    )


@pytest.mark.asyncio
async def test_scanner_persists_only_metadata_and_cursors(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=GitHubClient(),
        topic_client=TopicClient(),
    )
    result = await scanner.scan(since=NOW - timedelta(minutes=30), now=NOW)
    uow = RepositoryUnitOfWork(tmp_path, operation_id="scoped-scan")
    uow.register_external_evidence(())

    scanner.stage(result, uow)
    changed = uow.publish().changed_paths

    assert changed == (
        "data/state/scoped-source-observations.json",
        "data/state/source-cursors.json",
    )
    payload = json.loads(
        (tmp_path / "data/state/scoped-source-observations.json").read_text()
    )
    rendered = json.dumps(payload)
    assert "must not persist" not in rendered
    assert not (tmp_path / "data/github").exists()
    assert not (tmp_path / "data/magicians").exists()


class _DescribingGitHubClient(GitHubClient):
    async def get_json(self, path: str, params=None) -> list[Any] | dict[str, Any]:
        if path == "/orgs/trustless-ai/repos":
            if params["page"] > 1:
                return []
            return [
                {
                    "id": 1,
                    "name": "existing",
                    "full_name": "trustless-ai/existing",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                    "updated_at": "2026-08-28T02:00:00Z",
                    "description": "An existing repository.",
                },
                {
                    "id": 2,
                    "name": "primitives",
                    "full_name": "trustless-ai/primitives",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                    "updated_at": "2026-08-28T02:00:00Z",
                    "description": "Primitive contracts.",
                },
            ]
        return await super().get_json(path, params)


def _repository_index(*links: str) -> str:
    return (
        "---\n"
        "title: TAWG Knowledge Index\n"
        "type: index\n"
        "created: '2026-08-23'\n"
        "updated: '2026-08-23'\n"
        "---\n\n"
        "# TAWG Knowledge Index\n\n"
        "## Repositories\n\n"
        + "".join(f"{link}\n" for link in links)
        + "\n## Topics\n\n"
    )


@pytest.mark.asyncio
async def test_scan_captures_repository_descriptors(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=_DescribingGitHubClient(),
        topic_client=TopicClient(),
    )

    result = await scanner.scan(since=NOW - timedelta(minutes=30), now=NOW)

    assert {repo.name for repo in result.repositories} == {"existing", "primitives"}
    primitives = next(repo for repo in result.repositories if repo.name == "primitives")
    assert primitives.full_name == "trustless-ai/primitives"
    assert primitives.description == "Primitive contracts."


@pytest.mark.asyncio
async def test_repository_page_backfill_adds_missing_pages_and_index_links(
    tmp_path: Path,
) -> None:
    _scaffold(tmp_path)
    repos_root = tmp_path / "knowledge/repos"
    repos_root.mkdir(parents=True, exist_ok=True)
    (repos_root / "existing.md").write_text(
        "---\ntitle: existing\ntype: repository\ncreated: '2026-08-23'\n"
        "updated: '2026-08-23'\nprovenance_status: verified\n---\n\n# existing\n",
        encoding="utf-8",
    )
    index = tmp_path / "knowledge/index.md"
    index.write_text(
        _repository_index("- [[repos/existing|existing]]"),
        encoding="utf-8",
    )
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=None,
        topic_client=TopicClient(),
    )
    descriptors = (
        RepositoryDescriptor(
            name="existing",
            full_name="trustless-ai/existing",
            description="An existing repository.",
        ),
        RepositoryDescriptor(
            name="primitives",
            full_name="trustless-ai/primitives",
            description="Primitive contracts.",
        ),
    )
    uow = RepositoryUnitOfWork(tmp_path, operation_id="repo-backfill")
    uow.register_external_evidence(())

    scanner.stage_repository_pages(uow, descriptors, now=NOW)
    changed = uow.publish().changed_paths

    assert "knowledge/repos/primitives.md" in changed
    assert "knowledge/index.md" in changed
    assert "knowledge/repos/existing.md" not in changed
    primitives_page = (tmp_path / "knowledge/repos/primitives.md").read_text()
    assert 'title: "primitives"' in primitives_page
    assert "type: repository" in primitives_page
    assert "Primitive contracts." in primitives_page
    assert "https://github.com/trustless-ai/primitives" in primitives_page
    assert "[[repos/primitives|primitives]]" in index.read_text()


@pytest.mark.asyncio
async def test_repository_page_backfill_skips_when_index_is_missing(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=None,
        topic_client=TopicClient(),
    )
    descriptors = (
        RepositoryDescriptor(
            name="primitives",
            full_name="trustless-ai/primitives",
            description="Primitive contracts.",
        ),
    )
    uow = RepositoryUnitOfWork(tmp_path, operation_id="repo-backfill-missing-index")
    uow.register_external_evidence(())

    scanner.stage_repository_pages(uow, descriptors, now=NOW)
    changed = uow.publish().changed_paths

    assert changed == ()
    assert not (tmp_path / "knowledge/repos/primitives.md").exists()


@pytest.mark.asyncio
async def test_partial_scan_retains_last_verified_metadata_for_failed_source(
    tmp_path: Path,
) -> None:
    _scaffold(tmp_path)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=GitHubClient(),
        topic_client=TopicClient(),
    )
    first = await scanner.scan(since=NOW - timedelta(minutes=30), now=NOW)
    first_uow = RepositoryUnitOfWork(tmp_path, operation_id="scoped-scan-first")
    first_uow.register_external_evidence(())
    scanner.stage(first, first_uow)
    first_uow.publish()
    topic_before = next(
        item for item in first.observations if item.source_kind == "magicians_topic"
    )
    partial = ScopedScanResult(
        observations=tuple(
            item for item in first.observations if item.source_kind != "magicians_topic"
        ),
        github_cursors=first.github_cursors,
        magicians_cursors={},
        failed_sources=("magicians:27902",),
    )
    retry_uow = RepositoryUnitOfWork(tmp_path, operation_id="scoped-scan-retry")
    retry_uow.register_external_evidence(())

    scanner.stage(partial, retry_uow)
    retry_uow.publish()

    persisted = json.loads(
        (tmp_path / "data/state/scoped-source-observations.json").read_text()
    )
    assert topic_before.model_dump(mode="json") in persisted


def test_observation_requires_utc_and_result_rejects_duplicate_source_keys() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        ScopedObservation(
            source_key="active",
            source_kind="github_repository",
            source_locator="https://github.com/trustless-ai/active",
            updated_at=datetime(2026, 8, 28, 3),
            metadata_sha256="0" * 64,
        )

    observation = ScopedObservation(
        source_key="active",
        source_kind="github_repository",
        source_locator="https://github.com/trustless-ai/active",
        updated_at=NOW,
        metadata_sha256="0" * 64,
    )
    with pytest.raises(ValidationError, match="unique"):
        ScopedScanResult(
            observations=(observation, observation),
            github_cursors={},
            magicians_cursors={},
            failed_sources=(),
        )


def test_existing_observation_state_must_be_a_regular_file(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("[]\n", encoding="utf-8")
    observations = tmp_path / ScopedSourceScanner.OBSERVATIONS_PATH
    observations.symlink_to(outside)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=GitHubClient(),
        topic_client=TopicClient(),
    )
    result = ScopedScanResult(
        observations=(),
        github_cursors={},
        magicians_cursors={},
        failed_sources=(),
    )
    uow = RepositoryUnitOfWork(tmp_path, operation_id="scoped-scan-symlink")
    uow.register_external_evidence(())

    with pytest.raises(ScopedScanRejected, match="observation state"):
        scanner.stage(result, uow)


def test_existing_cursor_state_must_be_a_regular_file(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    cursors = tmp_path / ScopedSourceScanner.CURSORS_PATH
    cursors.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(SourceCursors().model_dump_json(), encoding="utf-8")
    cursors.symlink_to(outside)
    scanner = ScopedSourceScanner(
        tmp_path,
        github_client=GitHubClient(),
        topic_client=TopicClient(),
    )
    result = ScopedScanResult(
        observations=(),
        github_cursors={},
        magicians_cursors={},
        failed_sources=(),
    )
    uow = RepositoryUnitOfWork(tmp_path, operation_id="scoped-scan-cursor-symlink")
    uow.register_external_evidence(())

    with pytest.raises(ScopedScanRejected, match="cursor state"):
        scanner.stage(result, uow)
