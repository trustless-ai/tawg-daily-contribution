from __future__ import annotations

import json
from pathlib import Path

import pytest

from tawg_bot.delivery import (
    DeliveryAmbiguous,
    DeliveryFailed,
    DeliveryService,
)
from tawg_bot.telegram_api import SentMessage, TelegramApiError
from tests.unit.test_delivery import NOW, Api, Checkpoint, seed


class FailingApi(Api):
    async def send_message(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> SentMessage:
        self.calls.append((chat_id, text, reply_to_message_id))
        raise TelegramApiError("explicit Telegram failure")


def status(root: Path) -> str:
    return json.loads((root / "data/state/delivery-state.json").read_text())[0]["status"]


@pytest.mark.asyncio
async def test_explicit_api_failure_is_failed_and_safe_to_retry(tmp_path: Path) -> None:
    seed(tmp_path)
    with pytest.raises(DeliveryFailed):
        await DeliveryService(
            tmp_path, api=FailingApi(), chat_id=-10077, checkpoint=Checkpoint()
        ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)
    assert status(tmp_path) == "failed"

    api = Api()
    result = await DeliveryService(
        tmp_path, api=api, chat_id=-10077, checkpoint=Checkpoint()
    ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)

    assert result.status.value == "delivered"
    assert len(api.calls) == 1


@pytest.mark.asyncio
async def test_crash_after_send_recovers_as_ambiguous_without_resend(tmp_path: Path) -> None:
    seed(tmp_path)
    first_api = Api()

    def crash() -> None:
        raise RuntimeError("process stopped after Telegram accepted the message")

    with pytest.raises(RuntimeError, match="process stopped"):
        await DeliveryService(
            tmp_path,
            api=first_api,
            chat_id=-10077,
            checkpoint=Checkpoint(),
            after_send_hook=crash,
        ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)
    assert len(first_api.calls) == 1
    assert status(tmp_path) == "sending"

    second_api = Api()
    with pytest.raises(DeliveryAmbiguous):
        await DeliveryService(
            tmp_path, api=second_api, chat_id=-10077, checkpoint=Checkpoint()
        ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)

    assert not second_api.calls
    assert status(tmp_path) == "ambiguous"


@pytest.mark.asyncio
async def test_delivered_job_is_suppressed_and_content_change_is_rejected(tmp_path: Path) -> None:
    seed(tmp_path)
    first_api = Api()
    service = DeliveryService(
        tmp_path, api=first_api, chat_id=-10077, checkpoint=Checkpoint()
    )
    first = await service.deliver(
        job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW
    )

    second_api = Api()
    duplicate = await DeliveryService(
        tmp_path, api=second_api, chat_id=-10077, checkpoint=Checkpoint()
    ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)

    assert duplicate == first
    assert not second_api.calls
    with pytest.raises(DeliveryFailed, match="content"):
        await DeliveryService(
            tmp_path, api=second_api, chat_id=-10077, checkpoint=Checkpoint()
        ).deliver(job_id="daily:one", text="Changed", reply_to_message_id=None, now=NOW)


@pytest.mark.asyncio
async def test_returned_chat_mismatch_becomes_ambiguous(tmp_path: Path) -> None:
    seed(tmp_path)

    class WrongChat(Api):
        async def send_message(
            self, chat_id: int, text: str, reply_to_message_id: int | None = None
        ) -> SentMessage:
            self.calls.append((chat_id, text, reply_to_message_id))
            return SentMessage(message_id=88, chat_id=-999)

    with pytest.raises(DeliveryAmbiguous, match="destination"):
        await DeliveryService(
            tmp_path, api=WrongChat(), chat_id=-10077, checkpoint=Checkpoint()
        ).deliver(job_id="daily:one", text="Hello", reply_to_message_id=None, now=NOW)

    assert status(tmp_path) == "ambiguous"
