from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.github_announcements import (
    GitHubAnnouncementKind,
    GitHubAnnouncementState,
    GitHubIssueSnapshot,
    GitHubPullSnapshot,
    GitHubRepositorySnapshot,
    GitHubScanSnapshot,
    bootstrap_announcement_state,
    reconcile_announcements,
    render_announcement,
    resolve_referenced_pull_evidence,
)

NOW = datetime(2026, 8, 29, 15, tzinfo=UTC)


def pull(
    number: int,
    *,
    head: str,
    created_at: datetime | None = None,
    merged_at: datetime | None = None,
    title: str = "Add deterministic notices",
    author_login: str = "alice-dev",
) -> GitHubPullSnapshot:
    return GitHubPullSnapshot(
        number=number,
        title=title,
        author_login=author_login,
        head_sha=head,
        created_at=created_at or NOW - timedelta(hours=1),
        updated_at=merged_at or NOW,
        merged_at=merged_at,
        merge_commit_sha=("f" * 40 if merged_at is not None else None),
    )


def issue(
    number: int,
    *,
    created_at: datetime | None = None,
    title: str = "Document the release",
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=number,
        title=title,
        author_login="bob-dev",
        created_at=created_at or NOW,
        updated_at=NOW,
    )


def snapshot(
    *,
    open_pulls: tuple[GitHubPullSnapshot, ...] = (),
    recently_closed_pulls: tuple[GitHubPullSnapshot, ...] = (),
    open_issues: tuple[GitHubIssueSnapshot, ...] = (),
    repositories: tuple[GitHubRepositorySnapshot, ...] | None = None,
) -> GitHubScanSnapshot:
    return GitHubScanSnapshot(
        repositories=repositories
        or (
            GitHubRepositorySnapshot(
                full_name="trustless-ai/agent-sdk",
                open_pulls=open_pulls,
                recently_closed_pulls=recently_closed_pulls,
                open_issues=open_issues,
            ),
        )
    )


def write_state(root: Path, state: GitHubAnnouncementState) -> None:
    path = root / "data/state/github-announcement-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_unreferenced_question_does_not_load_invalid_github_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data/state/github-announcement-state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{invalid", encoding="utf-8")

    evidence = await resolve_referenced_pull_evidence(
        tmp_path,
        ("What are the ERC-8004 trust assumptions?",),
        now=NOW,
        client=None,
    )

    assert evidence == ()


@pytest.mark.asyncio
async def test_reference_limit_preserves_trigger_before_nearby_messages(
    tmp_path: Path,
) -> None:
    state = bootstrap_announcement_state(
        snapshot(
            open_pulls=tuple(
                pull(number, head=f"{number:040x}") for number in range(1, 17)
            )
        ),
        now=NOW,
    )
    write_state(tmp_path, state)

    evidence = await resolve_referenced_pull_evidence(
        tmp_path,
        (
            "Is agent-sdk#999 still waiting for me?",
            " ".join(f"agent-sdk#{number}" for number in range(1, 17)),
        ),
        now=NOW,
        client=None,
        max_items=16,
    )

    assert [item["number"] for item in evidence if "number" in item] == [
        999,
        *range(1, 16),
    ]
    assert evidence[-1] == {
        "kind": "github_pull_current_state_coverage_gap",
        "max_items": 16,
        "reason": "reference_limit_exceeded",
    }


@pytest.mark.asyncio
async def test_stale_reference_refresh_has_an_aggregate_timeout(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        bootstrap_announcement_state(
            snapshot(open_pulls=(pull(26, head="a" * 40),)),
            now=NOW - timedelta(hours=2),
        ),
    )

    class NeverReturnsClient:
        async def get_json(
            self,
            path: str,
            params: dict[str, object] | None = None,
        ) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    evidence = await asyncio.wait_for(
        resolve_referenced_pull_evidence(
            tmp_path,
            ("agent-sdk#26",),
            now=NOW,
            client=NeverReturnsClient(),
            refresh_timeout_seconds=0.01,
        ),
        timeout=0.2,
    )

    assert evidence == (
        {
            "kind": "github_pull_current_state_gap",
            "repository": "trustless-ai/agent-sdk",
            "number": 26,
            "checked_at": "2026-08-29T13:00:00Z",
            "reason": "l2_state_stale_or_incomplete",
        },
    )


@pytest.mark.asyncio
async def test_stale_refresh_preserves_fast_result_when_peer_times_out(
    tmp_path: Path,
) -> None:
    write_state(
        tmp_path,
        bootstrap_announcement_state(
            snapshot(
                open_pulls=(
                    pull(26, head="a" * 40),
                    pull(27, head="b" * 40),
                )
            ),
            now=NOW - timedelta(hours=2),
        ),
    )

    class PartiallyResponsiveClient:
        async def get_json(
            self,
            path: str,
            params: dict[str, object] | None = None,
        ) -> dict[str, object]:
            if path.endswith("/27"):
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            return {
                "number": 26,
                "title": "Current title",
                "state": "open",
                "created_at": "2026-08-29T12:00:00Z",
                "updated_at": "2026-08-29T14:55:00Z",
                "merged_at": None,
                "merge_commit_sha": None,
                "user": {"login": "alice-dev"},
                "head": {"sha": "c" * 40},
            }

    evidence = await resolve_referenced_pull_evidence(
        tmp_path,
        ("agent-sdk#26 and agent-sdk#27",),
        now=NOW,
        client=PartiallyResponsiveClient(),
        refresh_timeout_seconds=0.01,
    )

    assert evidence[0]["kind"] == "github_pull_current_state"
    assert evidence[0]["number"] == 26
    assert evidence[0]["head_sha"] == "c" * 40
    assert evidence[1]["kind"] == "github_pull_current_state_gap"
    assert evidence[1]["number"] == 27


@pytest.mark.asyncio
async def test_cancelling_resolver_reclaims_refresh_tasks(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        bootstrap_announcement_state(
            snapshot(
                open_pulls=(
                    pull(26, head="a" * 40),
                    pull(27, head="b" * 40),
                )
            ),
            now=NOW - timedelta(hours=2),
        ),
    )

    class CancellableClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.active = 0
            self.cancelled = 0

        async def get_json(
            self,
            path: str,
            params: dict[str, object] | None = None,
        ) -> dict[str, object]:
            self.active += 1
            if self.active == 2:
                self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.active -= 1

    client = CancellableClient()
    resolver = asyncio.create_task(
        resolve_referenced_pull_evidence(
            tmp_path,
            ("agent-sdk#26 and agent-sdk#27",),
            now=NOW,
            client=client,
            refresh_timeout_seconds=10,
        )
    )
    await client.started.wait()
    resolver.cancel()

    with pytest.raises(asyncio.CancelledError):
        await resolver

    assert client.active == 0
    assert client.cancelled == 2


def test_bootstrap_records_only_open_items_without_announcements() -> None:
    current = snapshot(
        open_pulls=(pull(10, head="a" * 40),),
        recently_closed_pulls=(pull(9, head="9" * 40, merged_at=NOW - timedelta(minutes=2)),),
        open_issues=(issue(11),),
    )

    state = bootstrap_announcement_state(current, now=NOW)

    repository = state.repositories[0]
    assert repository.full_name == "trustless-ai/agent-sdk"
    assert [item.model_dump(mode="json") for item in repository.pulls] == [
        {
            "number": 10,
            "title": "Add deterministic notices",
            "author_login": "alice-dev",
            "head_sha": "a" * 40,
            "state": "open",
            "updated_at": "2026-08-29T15:00:00Z",
            "merged_at": None,
            "merge_commit_sha": None,
        }
    ]
    assert repository.issue_numbers == (11,)


def test_reconcile_preserves_current_open_and_merged_pull_metadata() -> None:
    baseline = bootstrap_announcement_state(
        snapshot(
            open_pulls=(
                pull(26, head="a" * 40, title="Fix programKey validation"),
                pull(27, head="b" * 40, title="Close follow-up checks"),
            )
        ),
        now=NOW,
    )
    merged_at = NOW + timedelta(minutes=20)

    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(
                pull(26, head="c" * 40, title="Fix programKey validation"),
            ),
            recently_closed_pulls=(
                pull(
                    27,
                    head="b" * 40,
                    title="Close follow-up checks",
                    merged_at=merged_at,
                ),
            ),
        ),
        now=NOW + timedelta(minutes=30),
    )

    assert [item.model_dump(mode="json") for item in result.state.repositories[0].pulls] == [
        {
            "number": 26,
            "title": "Fix programKey validation",
            "author_login": "alice-dev",
            "head_sha": "c" * 40,
            "state": "open",
            "updated_at": "2026-08-29T15:00:00Z",
            "merged_at": None,
            "merge_commit_sha": None,
        },
        {
            "number": 27,
            "title": "Close follow-up checks",
            "author_login": "alice-dev",
            "head_sha": "b" * 40,
            "state": "merged",
            "updated_at": "2026-08-29T15:20:00Z",
            "merged_at": "2026-08-29T15:20:00Z",
            "merge_commit_sha": "f" * 40,
        },
    ]


def test_multiple_commits_in_one_scan_emit_one_update_and_later_head_emits_another() -> None:
    baseline = bootstrap_announcement_state(
        snapshot(open_pulls=(pull(10, head="a" * 40),)), now=NOW
    )

    first = reconcile_announcements(
        baseline,
        snapshot(open_pulls=(pull(10, head="c" * 40),)),
        now=NOW + timedelta(minutes=30),
    )
    second = reconcile_announcements(
        first.state,
        snapshot(open_pulls=(pull(10, head="d" * 40),)),
        now=NOW + timedelta(hours=1),
    )

    assert [event.kind for event in first.events] == [GitHubAnnouncementKind.PR_UPDATED]
    assert [event.kind for event in second.events] == [GitHubAnnouncementKind.PR_UPDATED]
    assert first.events[0].event_id != second.events[0].event_id


def test_merge_dominates_head_update_and_created_then_merged_emits_only_merge() -> None:
    baseline = bootstrap_announcement_state(
        snapshot(open_pulls=(pull(10, head="a" * 40),)), now=NOW
    )
    merged_at = NOW + timedelta(minutes=20)

    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(pull(10, head="b" * 40),),
            recently_closed_pulls=(
                pull(10, head="b" * 40, merged_at=merged_at),
                pull(
                    12,
                    head="c" * 40,
                    created_at=NOW + timedelta(minutes=5),
                    merged_at=merged_at,
                ),
            ),
        ),
        now=NOW + timedelta(minutes=30),
    )

    assert [(event.number, event.kind) for event in result.events] == [
        (10, GitHubAnnouncementKind.PR_MERGED),
        (12, GitHubAnnouncementKind.PR_MERGED),
    ]


def test_closed_unmerged_is_silent_and_reopen_is_not_new() -> None:
    baseline = bootstrap_announcement_state(
        snapshot(open_pulls=(pull(10, head="a" * 40),)), now=NOW
    )
    closed = reconcile_announcements(
        baseline,
        snapshot(
            recently_closed_pulls=(pull(10, head="a" * 40),),
        ),
        now=NOW + timedelta(minutes=30),
    )
    reopened = reconcile_announcements(
        closed.state,
        snapshot(open_pulls=(pull(10, head="a" * 40),)),
        now=NOW + timedelta(hours=1),
    )
    updated = reconcile_announcements(
        reopened.state,
        snapshot(open_pulls=(pull(10, head="b" * 40),)),
        now=NOW + timedelta(hours=1, minutes=30),
    )

    assert closed.events == ()
    assert reopened.events == ()
    assert [event.kind for event in updated.events] == [GitHubAnnouncementKind.PR_UPDATED]


def test_new_repository_is_baselined_and_only_still_open_new_issue_is_announced() -> None:
    baseline = bootstrap_announcement_state(snapshot(), now=NOW)
    later = NOW + timedelta(minutes=30)
    new_repository = GitHubRepositorySnapshot(
        full_name="trustless-ai/new-repo",
        open_pulls=(pull(1, head="1" * 40, created_at=later),),
        recently_closed_pulls=(),
        open_issues=(issue(2, created_at=later),),
    )

    discovered = reconcile_announcements(
        baseline,
        snapshot(repositories=(*snapshot().repositories, new_repository)),
        now=later,
    )
    with_issue = reconcile_announcements(
        discovered.state,
        snapshot(
            repositories=(
                *snapshot().repositories,
                new_repository.model_copy(
                    update={
                        "open_issues": (
                            *new_repository.open_issues,
                            issue(3, created_at=later + timedelta(minutes=10)),
                        )
                    }
                ),
            )
        ),
        now=later + timedelta(minutes=30),
    )

    assert discovered.events == ()
    assert [(event.number, event.kind) for event in with_issue.events] == [
        (3, GitHubAnnouncementKind.ISSUE_OPENED)
    ]


def test_unseen_old_reopened_pull_is_silent_but_a_truly_new_pull_is_announced() -> None:
    baseline: GitHubAnnouncementState = bootstrap_announcement_state(snapshot(), now=NOW)
    later = NOW + timedelta(minutes=30)

    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(
                pull(8, head="8" * 40, created_at=NOW - timedelta(days=2)),
                pull(9, head="9" * 40, created_at=NOW + timedelta(minutes=5)),
            )
        ),
        now=later,
    )

    assert [(event.number, event.kind) for event in result.events] == [
        (9, GitHubAnnouncementKind.PR_OPENED)
    ]


def test_events_at_the_previous_scan_second_are_not_lost() -> None:
    cursor = NOW + timedelta(microseconds=500_000)
    baseline = bootstrap_announcement_state(
        snapshot(open_pulls=(pull(10, head="a" * 40),)), now=cursor
    )

    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(pull(11, head="b" * 40, created_at=NOW),),
            recently_closed_pulls=(pull(10, head="c" * 40, merged_at=NOW),),
            open_issues=(issue(12, created_at=NOW),),
        ),
        now=NOW + timedelta(minutes=30),
    )

    assert [(event.number, event.kind) for event in result.events] == [
        (10, GitHubAnnouncementKind.PR_MERGED),
        (11, GitHubAnnouncementKind.PR_OPENED),
        (12, GitHubAnnouncementKind.ISSUE_OPENED),
    ]


def test_markdown_renderer_escapes_dynamic_labels_and_uses_github_links() -> None:
    baseline = bootstrap_announcement_state(snapshot(), now=NOW)
    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(
                pull(
                    9,
                    head="9" * 40,
                    created_at=NOW + timedelta(minutes=5),
                    title="Fix [parser](oops)\\now\nplease *fast*",
                ),
            )
        ),
        now=NOW + timedelta(minutes=30),
    )

    rendered = render_announcement(result.events[0])

    assert rendered == (
        "🆕 **New PR** in [trustless-ai/agent-sdk]"
        "(https://github.com/trustless-ai/agent-sdk)\n"
        "[#9 Fix \\[parser\\]\\(oops\\)\\\\now please \\*fast\\*]"
        "(https://github.com/trustless-ai/agent-sdk/pull/9)\n"
        "Opened by [alice-dev](https://github.com/alice-dev)"
    )
    assert "@alice-dev" not in rendered


def test_markdown_renderer_supports_and_url_encodes_github_bot_logins() -> None:
    baseline = bootstrap_announcement_state(snapshot(), now=NOW)
    result = reconcile_announcements(
        baseline,
        snapshot(
            open_pulls=(
                pull(
                    9,
                    head="9" * 40,
                    created_at=NOW + timedelta(minutes=5),
                    author_login="dependabot[bot]",
                ),
            )
        ),
        now=NOW + timedelta(minutes=30),
    )

    rendered = render_announcement(result.events[0])

    assert "[dependabot\\[bot\\]](https://github.com/dependabot%5Bbot%5D)" in rendered
