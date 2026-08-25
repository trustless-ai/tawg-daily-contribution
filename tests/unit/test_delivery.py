from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.delivery import DeliveryRejected, DeliveryService
from tawg_bot.telegram_api import SentMessage

PROJECT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 23, 5, tzinfo=UTC)


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
        return SentMessage(message_id=100 + len(self.calls), chat_id=chat_id)


class Checkpoint:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        del operation_id
        import json

        self.statuses.append(
            json.loads((root / "data/state/delivery-state.json").read_text())[0]["status"]
        )


def seed(root: Path) -> None:
    (root / "config").mkdir()
    (root / "config/privacy.yml").write_bytes((PROJECT / "config/privacy.yml").read_bytes())
    state = root / "data/state"
    state.mkdir(parents=True)
    (state / "delivery-state.json").write_text("[]\n", encoding="utf-8")
    (state / "pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_records_durable_intent_before_send_and_success_metadata(tmp_path: Path) -> None:
    seed(tmp_path)
    api = Api()
    checkpoint = Checkpoint()

    result = await DeliveryService(
        tmp_path, api=api, chat_id=-10077, checkpoint=checkpoint
    ).deliver(
        job_id="daily:2026-08-23T23:00:00Z",
        text="A grounded catch-up.",
        reply_to_message_id=None,
        message_thread_id=None,
        now=NOW,
    )

    assert checkpoint.statuses == ["prepared", "sending", "delivered"]
    assert api.calls == [(-10077, "A grounded catch-up.", None, None)]
    assert result.status.value == "delivered"
    assert result.telegram_chat_id == -10077
    assert result.telegram_message_ids == [101]
    assert result.sent_at == NOW


@pytest.mark.asyncio
async def test_split_reply_keeps_every_message_in_the_trigger_thread(tmp_path: Path) -> None:
    seed(tmp_path)
    api = Api()

    result = await DeliveryService(
        tmp_path, api=api, chat_id=-10077, checkpoint=Checkpoint()
    ).deliver(
        job_id="reply:tg:tawg:12",
        text="A" * 4000 + "\n\n" + "B" * 300,
        reply_to_message_id=12,
        message_thread_id=700,
        now=NOW,
    )

    assert result.message_count == 2
    assert len(api.calls) == 2
    assert api.calls[0][2] == 12
    assert api.calls[1][2] is None
    assert api.calls[0][3] == 700
    assert api.calls[1][3] == 700
    assert all(len(call[1]) <= 4096 for call in api.calls)


@pytest.mark.asyncio
async def test_split_keeps_both_messages_within_limit_when_early_paragraph_is_too_short(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    api = Api()
    text = "a" * 3_000 + "\n\n" + "b" * 900 + "\n" + "c" * 3_800

    await DeliveryService(tmp_path, api=api, chat_id=-10077, checkpoint=Checkpoint()).deliver(
        job_id="daily:long-natural-boundary",
        text=text,
        reply_to_message_id=None,
        message_thread_id=None,
        now=NOW,
    )

    assert len(api.calls) == 2
    assert all(len(call[1]) <= 4096 for call in api.calls)


@pytest.mark.asyncio
async def test_privacy_is_rescanned_before_any_send(tmp_path: Path) -> None:
    seed(tmp_path)
    api = Api()

    with pytest.raises(DeliveryRejected, match="privacy"):
        await DeliveryService(tmp_path, api=api, chat_id=-10077, checkpoint=Checkpoint()).deliver(
            job_id="reply:tg:tawg:12",
            text="Contact private@example.com",
            reply_to_message_id=12,
            message_thread_id=700,
            now=NOW,
        )

    assert not api.calls


@pytest.mark.asyncio
async def test_rejects_text_that_cannot_fit_two_messages(tmp_path: Path) -> None:
    seed(tmp_path)

    with pytest.raises(DeliveryRejected, match="two"):
        await DeliveryService(tmp_path, api=Api(), chat_id=-10077, checkpoint=Checkpoint()).deliver(
            job_id="oversized",
            text="x" * 8193,
            reply_to_message_id=None,
            message_thread_id=None,
            now=NOW,
        )
