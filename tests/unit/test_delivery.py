from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.delivery import DeliveryRejected, DeliveryService
from tawg_bot.models import BotRoute, JobStatus, PendingBotJob, RouteContextScope
from tawg_bot.persist_mode import PersistMode
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
    state = json.loads(
        (tmp_path / "data/state/delivery-state.json").read_text(encoding="utf-8")
    )
    assert state[0]["delivery_format"] == "rich_markdown_v1"


@pytest.mark.asyncio
async def test_rich_reply_keeps_legacy_over_4096_content_in_one_message(tmp_path: Path) -> None:
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

    assert result.message_count == 1
    assert len(api.calls) == 1
    assert api.calls[0][2] == 12
    assert api.calls[0][3] == 700
    assert len(api.calls[0][1]) > 4096


@pytest.mark.asyncio
async def test_reply_delivery_preserves_sparse_mutation_receipts(tmp_path: Path) -> None:
    seed(tmp_path)
    target = PendingBotJob(
        job_id="reply:tg:tawg:12",
        trigger_record_id="tg:tawg:12",
        reply_to_message_id=12,
        status=JobStatus.READY,
        classified_route=BotRoute.KNOWLEDGE_CORRECTION,
        router_context_scope=RouteContextScope.KNOWLEDGE,
        router_context_sha256="a" * 64,
        router_version="contextual-ai-v5",
        routed_at=NOW,
        prepared_reply_text="Recorded RVR.",
        prepared_language="en",
        knowledge_mutation_paths=["knowledge/topics/rvr.md"],
        knowledge_mutation_trigger_sha256="b" * 64,
        created_at=NOW,
        updated_at=NOW,
    )
    legacy = PendingBotJob(
        job_id="reply:tg:tawg:11",
        trigger_record_id="tg:tawg:11",
        reply_to_message_id=11,
        created_at=NOW,
        updated_at=NOW,
    )
    jobs_path = tmp_path / "data/state/pending-bot-jobs.json"
    jobs_path.write_text(
        json.dumps([legacy.persistence_payload(), target.persistence_payload()]) + "\n",
        encoding="utf-8",
    )

    await DeliveryService(
        tmp_path, api=Api(), chat_id=-10077, checkpoint=Checkpoint()
    ).deliver(
        job_id=target.job_id,
        text="Recorded RVR.",
        reply_to_message_id=12,
        message_thread_id=None,
        now=NOW,
    )

    persisted = {
        item["job_id"]: item
        for item in json.loads(jobs_path.read_text(encoding="utf-8"))
    }
    assert persisted[target.job_id]["knowledge_mutation_paths"] == [
        "knowledge/topics/rvr.md"
    ]
    assert persisted[target.job_id]["knowledge_mutation_trigger_sha256"] == "b" * 64
    assert "knowledge_mutation_paths" not in persisted[legacy.job_id]


@pytest.mark.asyncio
async def test_rich_daily_keeps_legacy_two_part_content_in_one_message(
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

    assert len(api.calls) == 1
    assert len(api.calls[0][1]) == len(text)


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
            text="x" * 65_537,
            reply_to_message_id=None,
            message_thread_id=None,
            now=NOW,
        )


class _NoopCheckpoint:
    async def publish(self, operation_id: str, root: Path) -> None:
        del operation_id, root


@pytest.mark.asyncio
async def test_receipt_only_first_delivery_tolerates_missing_bot_scoped_state(
    tmp_path: Path,
) -> None:
    """A receipt-only mirror worker has no ``delivery-state.<bot_id>.json`` until its first
    successful delivery. ``_load_attempts`` must treat that missing file as "no prior
    attempts" instead of raising ``DeliveryRejected`` -- the regression that made the dev bot
    crash on every reply-triggered verify."""
    seed(tmp_path)
    bot_state = tmp_path / "data/state/delivery-state.8750877254.json"
    assert not bot_state.exists()
    api = Api()

    result = await DeliveryService(
        tmp_path,
        api=api,
        chat_id=-10077,
        checkpoint=_NoopCheckpoint(),
        bot_id=8750877254,
        persist_mode=PersistMode.RECEIPT_ONLY,
    ).deliver(
        job_id="reply:tg:tawg:12",
        text="Verification result.",
        reply_to_message_id=12,
        message_thread_id=None,
        now=NOW,
    )

    assert result.status.value == "delivered"
    assert api.calls == [(-10077, "Verification result.", 12, None)]
    persisted = json.loads(bot_state.read_text(encoding="utf-8"))
    assert persisted[0]["delivery_id"] == "reply:tg:tawg:12"
    assert persisted[0]["status"] == "delivered"
