from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.bot_router import PreparedReply
from tawg_bot.daily import PreparedDaily
from tawg_bot.github_announcements import (
    GitHubAnnouncement,
    GitHubAnnouncementKind,
    PublicGitHubHttpClient,
)
from tawg_bot.runtime import RuntimeFailure, _LivePipeline
from tawg_bot.telegram_api import SentMessage, TelegramApiAmbiguousError
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
            return SimpleNamespace(failed_sources=(), repositories=())

        def stage(self, result: object, uow: Any) -> None:
            del result
            uow.stage_json("data/state/scoped-source-observations.json", [])

        def stage_repository_pages(
            self,
            uow: Any,
            repositories: object,
            now: object,
        ) -> None:
            del uow, repositories, now

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
async def test_delivery_sends_each_pending_announcement_to_configured_topic_and_acks_it(
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

        def report_stuck_if_due(self, stuck_event_ids: object, *, now: object) -> bool:
            del stuck_event_ids, now
            return False

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
    monkeypatch.setenv("TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID", "3788")
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
    assert [call[2:] for call in api.calls] == [(None, 3788), (None, 3788)]
    assert acknowledged == [event.event_id, issue_event.event_id]
    assert checkpoint.operations[-1].startswith("github-announcement-ack:")


@pytest.mark.asyncio
async def test_privacy_rejected_announcement_does_not_block_other_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)
    rejected = GitHubAnnouncement(
        event_id="github-announcement:issue_opened:" + "d" * 24,
        kind=GitHubAnnouncementKind.ISSUE_OPENED,
        repository="trustless-ai/agent-sdk",
        number=28,
        title="Contact private@example.com for access",
        author_login="alice-dev",
        occurred_at=NOW,
    )
    event = GitHubAnnouncement(
        event_id="github-announcement:pr_opened:" + "e" * 24,
        kind=GitHubAnnouncementKind.PR_OPENED,
        repository="trustless-ai/agent-sdk",
        number=29,
        title="Add CI gate",
        author_login="bob-dev",
        occurred_at=NOW,
    )
    acknowledged: list[str] = []
    dead_lettered: list[tuple[str, str]] = []

    class Announcements:
        def pending(self) -> tuple[GitHubAnnouncement, ...]:
            return (rejected, event)

        def acknowledge(self, event_id: str) -> None:
            acknowledged.append(event_id)

        def reject(self, event_id: str, reason_code: str, *, now: object) -> bool:
            del now
            dead_lettered.append((event_id, reason_code))
            return True

        def report_stuck_if_due(self, stuck_event_ids: object, *, now: object) -> bool:
            del stuck_event_ids, now
            return False

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
            return SentMessage(message_id=903, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setenv("TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID", "3788")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)
    checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=checkpoint, now=NOW)
        pipeline.github_announcements = Announcements()  # type: ignore[assignment]

        await pipeline.telegram_delivery()

    assert len(api.calls) == 1
    assert "**New PR**" in api.calls[0][1]
    assert dead_lettered == [(rejected.event_id, "unredacted_personal_data")]
    assert acknowledged == [rejected.event_id, event.event_id]


@pytest.mark.asyncio
async def test_privacy_rejected_reply_does_not_block_other_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)

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
            return SentMessage(message_id=904, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.prepared_replies = [
            PreparedReply(
                "reply:test:rejected", 41, 55, "Contact private@example.com", (), "en", False
            ),
            PreparedReply("reply:test:clean", 42, 55, "ordinary reply", (), "en", False),
        ]

        await pipeline.telegram_delivery()

    assert api.calls == [(-10077, "ordinary reply", 42, 55)]


@pytest.mark.asyncio
async def test_privacy_rejected_prepared_daily_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)

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
            return SentMessage(message_id=905, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.prepared_daily = PreparedDaily(
            "daily:2026-08-28T00:00:00Z",
            "Contact private@example.com",
            ("Contact private@example.com",),
            (),
            False,
        )

        await pipeline.telegram_delivery()

    assert api.calls == []


@pytest.mark.asyncio
async def test_invalid_announcement_topic_defers_github_but_keeps_other_delivery_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold(tmp_path)
    event = GitHubAnnouncement(
        event_id="github-announcement:pr_updated:" + "c" * 24,
        kind=GitHubAnnouncementKind.PR_UPDATED,
        repository="trustless-ai/agent-sdk",
        number=27,
        title="Tighten CI gate",
        author_login="alice-dev",
        occurred_at=NOW,
    )
    acknowledged: list[str] = []

    class Announcements:
        def pending(self) -> tuple[GitHubAnnouncement, ...]:
            return (event,)

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
            return SentMessage(message_id=902, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.delenv("TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID", raising=False)
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        pipeline.github_announcements = Announcements()  # type: ignore[assignment]
        pipeline.prepared_replies = [
            PreparedReply("reply:test", 41, 55, "ordinary reply", (), "en", False)
        ]

        await pipeline.telegram_delivery()

    assert api.calls == [(-10077, "ordinary reply", 41, 55)]
    assert acknowledged == []


@pytest.mark.parametrize("raw", [None, "not-a-topic", "0", "-1"])
def test_github_announcement_topic_rejects_missing_or_invalid_configuration(
    raw: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID"
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)

    with pytest.raises(RuntimeFailure, match=name):
        _LivePipeline._github_announcement_topic_id()


def seed_pending(root: Path, events: tuple[GitHubAnnouncement, ...]) -> None:
    (root / "data/state/pending-github-announcements.json").write_text(
        json.dumps([event.model_dump(mode="json") for event in events]) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_privacy_rejected_announcement_is_dead_lettered_and_logged_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    rejected = GitHubAnnouncement(
        event_id="github-announcement:issue_opened:" + "f" * 24,
        kind=GitHubAnnouncementKind.ISSUE_OPENED,
        repository="trustless-ai/agent-sdk",
        number=30,
        title="Contact private@example.com for access",
        author_login="alice-dev",
        occurred_at=NOW,
    )
    seed_pending(tmp_path, (rejected,))

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
            return SentMessage(message_id=906, chat_id=chat_id)

    api = Api()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setenv("TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID", "3788")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline.telegram_delivery()
        first_output = capsys.readouterr().out
        await pipeline.telegram_delivery()
        second_output = capsys.readouterr().out

    assert api.calls == []
    assert json.loads(
        (tmp_path / "data/state/pending-github-announcements.json").read_text()
    ) == []
    deadletter = json.loads(
        (tmp_path / "data/state/github-announcement-deadletter.json").read_text()
    )
    assert [item["event_id"] for item in deadletter] == [rejected.event_id]
    assert deadletter[0]["reason_code"] == "unredacted_personal_data"
    assert deadletter[0]["rejected_at"].endswith("Z")
    assert first_output.count("announcement_rejected") == 1
    assert "reason_code=unredacted_personal_data" in first_output
    assert "announcement_rejected" not in second_output


@pytest.mark.asyncio
async def test_stuck_announcements_are_not_resent_and_alert_is_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold(tmp_path)
    first = GitHubAnnouncement(
        event_id="github-announcement:pr_updated:" + "1" * 24,
        kind=GitHubAnnouncementKind.PR_UPDATED,
        repository="trustless-ai/zkIE",
        number=45,
        title="Soundness repair",
        author_login="alice-dev",
        occurred_at=NOW,
    )
    second = GitHubAnnouncement(
        event_id="github-announcement:pr_updated:" + "2" * 24,
        kind=GitHubAnnouncementKind.PR_UPDATED,
        repository="trustless-ai/zkIE",
        number=47,
        title="Proving stack refactor",
        author_login="bob-dev",
        occurred_at=NOW,
    )
    seed_pending(tmp_path, (first, second))

    class AmbiguousApi:
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
            raise TelegramApiAmbiguousError("Telegram outcome is unknown")

    api = AmbiguousApi()
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-10077")
    monkeypatch.setenv("TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID", "3788")
    monkeypatch.setattr(runtime_module.TelegramApi, "from_env", lambda **kwargs: api)

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(tmp_path, client=client, checkpoint=Checkpoint(), now=NOW)
        await pipeline.telegram_delivery()
        first_output = capsys.readouterr().out
        assert len(api.calls) == 2

        pipeline.now = NOW + timedelta(minutes=1)
        await pipeline.telegram_delivery()
        second_output = capsys.readouterr().out
        assert len(api.calls) == 2

        pipeline.now = NOW + timedelta(hours=6, minutes=1)
        await pipeline.telegram_delivery()
        third_output = capsys.readouterr().out
        assert len(api.calls) == 2

    assert first_output.count("announcement_stuck") == 1
    assert "count=2" in first_output
    assert "telegram_outcome_unknown:2" in first_output
    assert "announcement_stuck" not in second_output
    assert third_output.count("announcement_stuck") == 1
    assert json.loads(
        (tmp_path / "data/state/pending-github-announcements.json").read_text()
    ) == [first.model_dump(mode="json"), second.model_dump(mode="json")]


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
