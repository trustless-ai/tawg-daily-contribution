from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.runtime import _LivePipeline
from tawg_bot.storage import JsonlCollection
from tawg_bot.telegram_api import SentMessage
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CHAT_ID = -100_424_242


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        self.operations.append(operation_id)


class AskQuestionAi:
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["job_type"] == "ask_question"
        return {
            "schema_version": "tawg.ask_question-result.v1",
            "question_text": "whoa, what did I miss just now?",
        }


class TelegramApi:
    calls: ClassVar[list[tuple[int, str, int | None, int | None]]] = []

    @classmethod
    def from_env(cls, *, client: Any) -> TelegramApi:
        del client
        return cls()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SentMessage:
        self.calls.append((chat_id, text, reply_to_message_id, message_thread_id))
        return SentMessage(message_id=6000 + len(self.calls), chat_id=chat_id)


def seed_messages(root: Path, timestamps: list[datetime]) -> None:
    path = root / "data/telegram/2026/09/messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        SourceRecord.from_text(
            record_id=f"tg:tawg:{i}",
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator=f"repo:data/telegram/2026/09/messages.jsonl#tg:tawg:{i}",
            author_person_id="alice",
            author_source_handle="alice",
            created_at=ts,
            updated_at=ts,
            text_original="hello",
            ingested_at=ts,
        )
        for i, ts in enumerate(timestamps)
    ]
    path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes(records))


@pytest.mark.asyncio
async def test_busy_question_sends_and_stages_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    seed_messages(
        tmp_path,
        [NOW - timedelta(minutes=m) for m in (5, 10, 15, 20, 25, 30)],
    )
    ai = AskQuestionAi()
    checkpoint = Checkpoint()
    TelegramApi.calls = []
    monkeypatch.setattr("tawg_bot.runtime.TelegramApi", TelegramApi)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", str(CHAT_ID))
    monkeypatch.setenv("TAWG_BUSY_QUESTION_THRESHOLD", "5")

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=checkpoint,
            now=NOW,
            ai=ai,  # type: ignore[arg-type]
        )
        await pipeline.maybe_ask_busy_question(NOW)

    assert len(TelegramApi.calls) == 1
    assert TelegramApi.calls[0][0] == CHAT_ID
    assert TelegramApi.calls[0][1].startswith("@Tmerlini_bot")
    assert "what did I miss" in TelegramApi.calls[0][1]
    state = json.loads(
        (tmp_path / "data/state/busy-question-state.json").read_text(encoding="utf-8")
    )
    assert state["last_triggered_at"] == NOW.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_busy_question_skips_when_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    seed_messages(tmp_path, [NOW - timedelta(minutes=5)])
    ai = AskQuestionAi()
    checkpoint = Checkpoint()
    TelegramApi.calls = []
    monkeypatch.setattr("tawg_bot.runtime.TelegramApi", TelegramApi)
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", str(CHAT_ID))
    monkeypatch.setenv("TAWG_BUSY_QUESTION_THRESHOLD", "5")

    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=checkpoint,
            now=NOW,
            ai=ai,  # type: ignore[arg-type]
        )
        await pipeline.maybe_ask_busy_question(NOW)

    assert TelegramApi.calls == []
    assert not (tmp_path / "data/state/busy-question-state.json").exists()
