from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.github_announcements import (
    GitHubAnnouncementKind,
    GitHubAnnouncementRejected,
    GitHubAnnouncementScanner,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork

NOW = datetime(2026, 8, 29, 15, tzinfo=UTC)


class GitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.head = "a" * 40
        self.issue_numbers = [7]
        self.closed: list[dict[str, Any]] = []
        self.include_new_repository = False

    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> list[Any] | dict[str, Any]:
        self.calls.append((path, params))
        assert params is not None
        page = params["page"]
        if page != 1:
            return []
        if path == "/orgs/trustless-ai/repos":
            repositories = [
                {
                    "id": 1,
                    "name": "agent-sdk",
                    "full_name": "trustless-ai/agent-sdk",
                    "private": False,
                },
                {
                    "id": 2,
                    "name": "private",
                    "full_name": "trustless-ai/private",
                    "private": True,
                },
            ]
            if self.include_new_repository:
                repositories.append(
                    {
                        "id": 3,
                        "name": "new-repo",
                        "full_name": "trustless-ai/new-repo",
                        "private": False,
                    }
                )
            return repositories
        if path == "/repos/trustless-ai/agent-sdk/pulls":
            if params["state"] == "closed":
                return self.closed
            return [self._pull(5, head=self.head, created_at=NOW - timedelta(days=1))]
        if path == "/repos/trustless-ai/agent-sdk/issues":
            return [
                *[
                    self._issue(
                        number,
                        created_at=(
                            NOW + timedelta(minutes=10) if number == 8 else NOW - timedelta(days=1)
                        ),
                    )
                    for number in self.issue_numbers
                ],
                {
                    **self._issue(99, created_at=NOW),
                    "pull_request": {"url": "https://api.github.com/pulls/99"},
                },
            ]
        if path == "/repos/trustless-ai/new-repo/pulls":
            if params["state"] == "closed":
                raise AssertionError("new repositories must not scan closed PR history")
            return [self._pull(1, head="1" * 40, created_at=NOW)]
        if path == "/repos/trustless-ai/new-repo/issues":
            return [self._issue(2, created_at=NOW)]
        raise AssertionError(path)

    @staticmethod
    def _pull(number: int, *, head: str, created_at: datetime) -> dict[str, Any]:
        return {
            "number": number,
            "state": "open",
            "title": "Add notices",
            "user": {"login": "alice-dev"},
            "head": {"sha": head},
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": NOW.isoformat().replace("+00:00", "Z"),
            "merged_at": None,
            "merge_commit_sha": None,
        }

    @staticmethod
    def _issue(number: int, *, created_at: datetime) -> dict[str, Any]:
        return {
            "number": number,
            "state": "open",
            "title": "Document notices",
            "user": {"login": "bob-dev"},
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": NOW.isoformat().replace("+00:00", "Z"),
        }


def publish(root: Path, scanner: GitHubAnnouncementScanner, batch: object) -> tuple[str, ...]:
    uow = RepositoryUnitOfWork(root, operation_id="github-announcements")
    uow.register_external_evidence(())
    scanner.stage(batch, uow)  # type: ignore[arg-type]
    return uow.publish().changed_paths


@pytest.mark.asyncio
async def test_bootstrap_uses_open_items_only_and_stages_an_empty_queue(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)

    batch = await scanner.bootstrap(now=NOW)
    changed = publish(tmp_path, scanner, batch)

    assert changed == (
        "data/state/github-announcement-state.json",
        "data/state/pending-github-announcements.json",
    )
    assert not any(
        path.endswith("/pulls") and params is not None and params.get("state") == "closed"
        for path, params in client.calls
    )
    state = json.loads((tmp_path / "data/state/github-announcement-state.json").read_text())
    assert state["repositories"][0]["issue_numbers"] == [7]
    assert state["repositories"][0]["pulls"][0]["number"] == 5
    assert json.loads((tmp_path / "data/state/pending-github-announcements.json").read_text()) == []


@pytest.mark.asyncio
async def test_incremental_scan_queues_head_issue_and_merge_events_once(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    client.head = "b" * 40
    client.issue_numbers.append(8)
    merged_at = NOW + timedelta(minutes=20)
    client.closed = [
        {
            **client._pull(6, head="6" * 40, created_at=NOW + timedelta(minutes=5)),
            "state": "closed",
            "updated_at": merged_at.isoformat().replace("+00:00", "Z"),
            "merged_at": merged_at.isoformat().replace("+00:00", "Z"),
            "merge_commit_sha": "f" * 40,
        }
    ]

    batch = await scanner.scan(now=NOW + timedelta(minutes=30))
    publish(tmp_path, scanner, batch)
    retry = await scanner.scan(now=NOW + timedelta(hours=1))

    assert [event.kind for event in batch.pending] == [
        GitHubAnnouncementKind.PR_UPDATED,
        GitHubAnnouncementKind.ISSUE_OPENED,
        GitHubAnnouncementKind.PR_MERGED,
    ]
    assert retry.pending == batch.pending


@pytest.mark.asyncio
async def test_incremental_scan_includes_merge_at_exact_cursor_second(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    cursor = NOW + timedelta(microseconds=500_000)
    publish(tmp_path, scanner, await scanner.bootstrap(now=cursor))
    client.closed = [
        {
            **client._pull(6, head="6" * 40, created_at=NOW),
            "state": "closed",
            "updated_at": NOW.isoformat().replace("+00:00", "Z"),
            "merged_at": NOW.isoformat().replace("+00:00", "Z"),
            "merge_commit_sha": "f" * 40,
        }
    ]

    batch = await scanner.scan(now=NOW + timedelta(minutes=30))

    assert [(event.number, event.kind) for event in batch.pending] == [
        (6, GitHubAnnouncementKind.PR_MERGED)
    ]


@pytest.mark.asyncio
async def test_incremental_scan_uses_head_sha_when_merged_pr_omits_merge_sha(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    merged_at = NOW + timedelta(minutes=20)
    client.closed = [
        {
            **client._pull(6, head="6" * 40, created_at=NOW + timedelta(minutes=5)),
            "state": "closed",
            "updated_at": merged_at.isoformat().replace("+00:00", "Z"),
            "merged_at": merged_at.isoformat().replace("+00:00", "Z"),
            "merge_commit_sha": None,
        }
    ]

    batch = await scanner.scan(now=NOW + timedelta(minutes=30))

    assert [(event.number, event.kind) for event in batch.pending] == [
        (6, GitHubAnnouncementKind.PR_MERGED)
    ]
    assert batch.state.repositories[0].pulls[-1].merge_commit_sha == "6" * 40


@pytest.mark.asyncio
async def test_incremental_scan_does_not_repeat_merge_when_api_adds_merge_sha(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    merged_at = NOW + timedelta(minutes=20)
    merged_pull = {
        **client._pull(6, head="6" * 40, created_at=NOW + timedelta(minutes=5)),
        "state": "closed",
        "updated_at": merged_at.isoformat().replace("+00:00", "Z"),
        "merged_at": merged_at.isoformat().replace("+00:00", "Z"),
        "merge_commit_sha": None,
    }
    client.closed = [merged_pull]
    first = await scanner.scan(now=merged_at + timedelta(microseconds=500))
    publish(tmp_path, scanner, first)
    client.closed = [
        {
            **merged_pull,
            "updated_at": (merged_at + timedelta(seconds=30))
            .isoformat()
            .replace("+00:00", "Z"),
            "merge_commit_sha": "f" * 40,
        }
    ]

    second = await scanner.scan(now=NOW + timedelta(minutes=50))

    assert [event.event_id for event in second.pending] == [first.pending[0].event_id]
    assert second.state.repositories[0].pulls[-1].merge_commit_sha == "f" * 40


@pytest.mark.asyncio
async def test_closed_pull_pagination_does_not_stop_on_a_full_boundary_page(
    tmp_path: Path,
) -> None:
    class PagingClient(GitHubClient):
        async def get_json(
            self, path: str, params: dict[str, object] | None = None
        ) -> list[Any] | dict[str, Any]:
            assert params is not None
            if path == "/repos/trustless-ai/agent-sdk/pulls" and params["state"] == "closed":
                if params["page"] == 1:
                    return [
                        {
                            **self._pull(
                                number,
                                head=f"{number:040x}",
                                created_at=NOW - timedelta(days=1),
                            ),
                            "state": "closed",
                            "updated_at": NOW.isoformat().replace("+00:00", "Z"),
                        }
                        for number in range(100, 200)
                    ]
                if params["page"] == 2:
                    return [
                        {
                            **self._pull(6, head="6" * 40, created_at=NOW),
                            "state": "closed",
                            "updated_at": NOW.isoformat().replace("+00:00", "Z"),
                            "merged_at": NOW.isoformat().replace("+00:00", "Z"),
                            "merge_commit_sha": "f" * 40,
                        }
                    ]
                return []
            return await super().get_json(path, params)

    client = PagingClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))

    batch = await scanner.scan(now=NOW + timedelta(minutes=30))

    assert [(event.number, event.kind) for event in batch.pending] == [
        (6, GitHubAnnouncementKind.PR_MERGED)
    ]


@pytest.mark.asyncio
async def test_new_public_repository_is_silently_baselined(tmp_path: Path) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    client.include_new_repository = True

    batch = await scanner.scan(now=NOW + timedelta(minutes=30))

    assert batch.pending == ()
    assert any(item.full_name == "trustless-ai/new-repo" for item in batch.state.repositories)


@pytest.mark.asyncio
async def test_acknowledge_removes_only_the_delivered_event(tmp_path: Path) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    client.head = "b" * 40
    client.issue_numbers.append(8)
    batch = await scanner.scan(now=NOW + timedelta(minutes=30))
    publish(tmp_path, scanner, batch)

    scanner.acknowledge(batch.pending[0].event_id)

    assert scanner.pending() == batch.pending[1:]


@pytest.mark.asyncio
async def test_invalid_or_incomplete_github_page_fails_without_state_change(
    tmp_path: Path,
) -> None:
    client = GitHubClient()
    scanner = GitHubAnnouncementScanner(tmp_path, client=client)
    publish(tmp_path, scanner, await scanner.bootstrap(now=NOW))
    before = (tmp_path / "data/state/github-announcement-state.json").read_bytes()

    async def invalid(path: str, params=None):
        del path, params
        return {"unexpected": "mapping"}

    client.get_json = invalid  # type: ignore[method-assign]

    with pytest.raises(GitHubAnnouncementRejected, match="incomplete"):
        await scanner.scan(now=NOW + timedelta(minutes=30))

    assert (tmp_path / "data/state/github-announcement-state.json").read_bytes() == before
