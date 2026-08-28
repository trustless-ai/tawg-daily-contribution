from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from tawg_bot.models import PendingBotJob
from tawg_bot.runtime import _LivePipeline
from tawg_bot.telegram_api import SentMessage
from tawg_bot.telegram_intake import ingest_envelopes
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 28, 6, 13, tzinfo=UTC)
CHAT_ID = -100_424_242


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


class GreetingAi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(deepcopy(kwargs))
        context = kwargs["context_pack"]
        assert "update_id" not in context
        assert "998810840" not in context
        assert "998810841" not in context
        assert "message_thread_id" not in context
        assert "reply_to_message_id" not in context
        if kwargs["job_type"] == "route":
            return {
                "schema_version": "tawg.route-result.v2",
                "route": "coordination",
                "context_scope": "conversation",
            }
        return {
            "schema_version": "tawg.reply-result.v2",
            "reply_text": "Good morning — glad to see you both!",
            "language": "en",
            "english_recap": None,
            "citations": [],
            "evidence_status": "not_verified",
            "verification_gaps": [],
            "correction_transaction": None,
            "refusal": False,
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
        return SentMessage(message_id=4000 + len(self.calls), chat_id=chat_id)


def greeting(
    *,
    update_id: int,
    message_id: int,
    text: str,
    reply_to_message_id: int | None = None,
) -> TelegramWebhookEnvelope:
    envelope = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=False,
        text=text,
        public_username="alice_tawg",
        display_name="Alice",
        reply_to_message_id=reply_to_message_id,
        triggers_reply=False,
        integrity_digest="0" * 64,
    )
    payload = envelope.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return envelope.model_copy(update={"integrity_digest": digest})


@pytest.mark.asyncio
async def test_real_webhook_greetings_cross_the_full_reply_and_delivery_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    first = greeting(
        update_id=998_810_840,
        message_id=3513,
        text="Good morning!🌞 Catching up!",
    )
    second = greeting(
        update_id=998_810_841,
        message_id=3517,
        text="Good morning 🌞",
        reply_to_message_id=3513,
    )
    intake = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(first, second),
        now=NOW,
        telegram_chat_id=CHAT_ID,
    )
    assert intake.jobs_created == 2

    ai = GreetingAi()
    checkpoint = Checkpoint()
    TelegramApi.calls = []
    monkeypatch.setattr("tawg_bot.runtime.TelegramApi", TelegramApi)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", str(CHAT_ID))
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=checkpoint,
            now=NOW,
            ai=ai,  # type: ignore[arg-type]
        )
        await pipeline.publish_repository()
        await pipeline.telegram_delivery()

    assert [call[2:] for call in TelegramApi.calls] == [(3513, None), (3517, None)]
    assert [call[0] for call in TelegramApi.calls] == [CHAT_ID, CHAT_ID]
    assert all("Good morning" in call[1] for call in TelegramApi.calls)
    assert [call["job_type"] for call in ai.calls] == [
        "route",
        "reply",
        "route",
        "reply",
    ]
    second_route = json.loads(
        next(
            call["context_pack"]
            for call in ai.calls
            if call["operation_id"] == "reply:tg:tawg:3517:route"
        )
    )
    assert second_route["prior_messages"][-1]["record_id"] == "tg:tawg:3513"
    jobs = {
        job.job_id: job
        for job in (
            PendingBotJob.model_validate(item)
            for item in json.loads(
                (tmp_path / "data/state/pending-bot-jobs.json").read_text(
                    encoding="utf-8"
                )
            )
        )
    }
    assert jobs["reply:tg:tawg:3513"].status.value == "delivered"
    assert jobs["reply:tg:tawg:3517"].status.value == "delivered"
    attempts = json.loads(
        (tmp_path / "data/state/delivery-state.json").read_text(encoding="utf-8")
    )
    assert [attempt["reply_to_message_id"] for attempt in attempts] == [3513, 3517]
    assert all(attempt["message_thread_id"] is None for attempt in attempts)
