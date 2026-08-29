from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.github_announcements import (
    GitHubAnnouncement,
    GitHubAnnouncementKind,
    PublicGitHubHttpClient,
)
from tawg_bot.runtime import RuntimeFailure, _LivePipeline
from tawg_bot.telegram_api import SentMessage
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 29, 15, tzinfo=UTC)


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


def scaffold(root: Path) -> None:
    copy_static_runtime_tree(ROOT, root)
    (root / "data/state/github-announcement-state.json").write_text(
        json.dumps(
            {
                "schema_version": "tawg.github-announcement-state.v1",
                "initialized_at": "2026-08-29T14:00:00Z",
                "repositories": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "data/state/pending-github-announcements.json").write_text("[]\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_source_check_stages_scoped_and_announcement_state_atomically(
    tmp_path: Path,
) -> None:
    scaffold(tmp_path)
    checkpoint = Checkpoint()

    class Scoped:
        async def scan(self, **kwargs: Any) -> object:
            del kwargs
            return SimpleNamespace(failed_sources=())

        def stage(self, result: object, uow: Any) -> None:
            del result
            uow.stage_json("data/state/scoped-source-observations.json", [])

    class Announcements:
        async def scan(self, **kwargs: Any) -> object:
            del kwargs
            return SimpleNamespace(state=SimpleNamespace(), pending=())

        def stage(self, batch: object, uow: Any) -> None:
            del batch
            uow.stage_json(
                "data/state/github-announcement-state.json",
                {
                    "schema_version": "tawg.github-announcement-state.v1",
                    "initialized_at": "2026-08-29T14:00:00Z",
                    "repositories": [],
                },
            )
            uow.stage_json("data/state/pending-github-announcements.json", [])

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        pipeline.scoped_scanner = Scoped()  # type: ignore[assignment]
        pipeline.github_announcements = Announcements()  # type: ignore[assignment]

        await pipeline.source_check(NOW)

    assert pipeline.source_checked_at == NOW
    assert checkpoint.operations == [f"scoped-source-check:{int(NOW.timestamp())}"]


@pytest.mark.asyncio
async def test_source_failure_does_not_scan_or_advance_announcement_state(
    tmp_path: Path,
) -> None:
    scaffold(tmp_path)
    baseline = (tmp_path / "data/state/github-announcement-state.json").read_bytes()

    class Scoped:
        async def scan(self, **kwargs: Any) -> object:
            del kwargs
            return SimpleNamespace(failed_sources=("github:trustless-ai",))

        def stage(self, result: object, uow: Any) -> None:
            raise AssertionError("failed scan must not stage")

    class Announcements:
        async def scan(self, **kwargs: Any) -> object:
            raise AssertionError("failed scoped scan must short-circuit announcements")

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.scoped_scanner = Scoped()  # type: ignore[assignment]
        pipeline.github_announcements = Announcements()  # type: ignore[assignment]

        with pytest.raises(RuntimeFailure, match="incomplete"):
            await pipeline.source_check(NOW)

    assert (tmp_path / "data/state/github-announcement-state.json").read_bytes() == baseline


@pytest.mark.asyncio
async def test_delivery_sends_each_pending_announcement_as_top_level_markdown_and_acks_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)
    event = GitHubAnnouncement(
        event_id="github-announcement:pr_opened:" + "a" * 24,
        kind=GitHubAnnouncementKind.PR_OPENED,
        repository="trustless-ai/agent-sdk",
        number=25,
        title="Add CI gate",
        author_login="alice-dev",
        occurred_at=NOW,
    )
    issue_event = GitHubAnnouncement(
        event_id="github-announcement:issue_opened:" + "b" * 24,
        kind=GitHubAnnouncementKind.ISSUE_OPENED,
        repository="trustless-ai/agent-sdk",
        number=26,
        title="Document the gate",
        author_login="bob-dev",
        occurred_at=NOW,
    )
    acknowledged: list[str] = []

    class Announcements:
        def pending(self) -> tuple[GitHubAnnouncement, ...]:
            return (event, issue_event)

        def acknowledge(self, event_id: str) -> None:
            acknowledged.append(event_id)

    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str, int | None, int | None]] = []

        async def send_message(
            self,
            chat_id: int,
            text: str,
            reply_to_message_id: int | None = None,
            message_thread_id: int | None = None,
        ) -> SentMessage:
            self.calls.append((chat_id, text, reply_to_message_id, message_thread_id))
            return SentMessage(message_id=901, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)
    checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        pipeline.github_announcements = Announcements()  # type: ignore[assignment]

        await pipeline.telegram_delivery()

    assert len(api.calls) == 2
    assert [call[0] for call in api.calls] == [-10077, -10077]
    assert "**New PR**" in api.calls[0][1]
    assert "**New issue**" in api.calls[1][1]
    assert [call[2:] for call in api.calls] == [(None, None), (None, None)]
    assert acknowledged == [event.event_id, issue_event.event_id]
    assert checkpoint.operations[-1].startswith("github-announcement-ack:")


@pytest.mark.asyncio
async def test_public_bootstrap_client_omits_authorization_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await PublicGitHubHttpClient(client=client).get_json(
            "/orgs/trustless-ai/repos", {"page": 1}
        )

    assert payload == []
    assert "authorization" not in requests[0].headers
    assert requests[0].headers["accept"] == "application/vnd.github+json"
