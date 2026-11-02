from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.query import TelegramQuery
from tawg_bot.repository_session import RepositoryConflict
from tawg_bot.runtime import _LivePipeline
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
CHAT_ID = -100424242


class AckingPollingApi:
    """Telegram confirms updates below the offset of each subsequent request."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = list(updates)
        self.calls: list[int] = []
        self.confirmed_below = 0

    async def get_updates_page(
        self, offset: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.calls.append(offset)
        self.confirmed_below = max(self.confirmed_below, offset)
        self.updates = [
            update for update in self.updates if update["update_id"] >= offset
        ]
        return self.updates[:limit]


class Checkpoint:
    def __init__(self, *, fail_on_messages: bool = False) -> None:
        self.fail_on_messages = fail_on_messages
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)
        if self.fail_on_messages and operation_id.startswith(
            "telegram-intake-messages:"
        ):
            self.fail_on_messages = False
            raise RepositoryConflict


def _polling_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1000 + update_id,
            "date": int(NOW.timestamp()),
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "TAWG"},
            "from": {
                "id": 1042,
                "is_bot": False,
                "first_name": "Member",
                "username": "member_42",
            },
            "text": f"ordinary polling message {update_id}",
            "entities": [],
        },
    }


def _cursor(root: Path) -> int:
    return json.loads(
        (root / "data/state/source-cursors.json").read_text(encoding="utf-8")
    )["telegram_offset"]


def _clone_durable_tree(source: Path, target: Path) -> None:
    target.mkdir()
    for relative in ("config", "knowledge", "data", "prompts", "bot-skill"):
        shutil.copytree(source / relative, target / relative)
    shutil.copytree(
        source / "src/tawg_bot/schemas", target / "src/tawg_bot/schemas"
    )


@pytest.mark.asyncio
async def test_polling_cursor_waits_for_message_checkpoint_and_survives_new_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", str(CHAT_ID))
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.delenv("TAWG_ENTRY_CANDIDATES_ENABLED", raising=False)
    updates = [_polling_update(update_id) for update_id in range(1, 121)]
    api = AckingPollingApi(updates)

    class ApiFactory:
        @classmethod
        def from_env(cls, *, client: object) -> AckingPollingApi:
            del cls, client
            return api

    monkeypatch.setattr(runtime_module, "TelegramApi", ApiFactory)
    failing_checkpoint = Checkpoint(fail_on_messages=True)

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=failing_checkpoint,
            now=NOW,
        )
        with pytest.raises(RepositoryConflict):
            await pipeline.telegram_intake(NOW)

    # The failed message checkpoint must leave the durable cursor at zero; the
    # first getUpdates request (offset 0) is not a confirmation of page one.
    assert _cursor(tmp_path) == 0
    assert api.calls == [0]
    assert api.confirmed_below == 0

    retry_checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        retry_pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=retry_checkpoint,
            now=NOW,
        )
        await retry_pipeline.telegram_intake(NOW)

    # Same-directory retry replays page one; only after its messages are
    # checkpointed does the local cursor advance to 101.
    assert api.calls == [0, 0]
    assert api.confirmed_below == 0
    assert _cursor(tmp_path) == 101

    # A new clone sees the durable messages and advanced cursor; its first request
    # may now confirm page one and fetch page two without losing page one.
    new_clone = tmp_path / "new-clone"
    _clone_durable_tree(tmp_path, new_clone)
    clone_checkpoint = Checkpoint()
    async with httpx.AsyncClient() as client:
        clone_pipeline = _LivePipeline(
            new_clone,
            client=client,
            checkpoint=clone_checkpoint,
            now=NOW,
        )
        await clone_pipeline.telegram_intake(NOW)

    assert api.calls == [0, 0, 101]
    assert api.confirmed_below == 101
    assert _cursor(new_clone) == 121
    assert len(
        [
            record
            for record in TelegramQuery(new_clone).records()
            if record.record_id.startswith("tg:tawg:")
        ]
    ) == 120
