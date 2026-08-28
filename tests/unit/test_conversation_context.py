from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tawg_bot.conversation_context import ConversationContextBuilder
from tawg_bot.models import Relation, SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 27, 1, tzinfo=UTC)


def _record(
    message_id: int,
    text: str,
    created_at: datetime,
    *,
    group: str = "tawg",
    thread_id: int | None = 7,
    reply_to: int | None = None,
) -> SourceRecord:
    record_id = f"tg:{group}:{message_id}"
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/2026/08/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="@alice",
        created_at=created_at,
        updated_at=created_at,
        text_original=text,
        ingested_at=created_at,
        relations=(
            [
                Relation(
                    relation_type="reply_to",
                    target_record_id=f"tg:{group}:{reply_to}",
                )
            ]
            if reply_to is not None
            else []
        ),
        source_payload={"message_thread_id": thread_id},
    )


def test_routing_context_is_split_at_each_mention_boundary() -> None:
    chain_parent = _record(80, "The proposal is RVR.", NOW - timedelta(hours=3))
    ordinary_prior = _record(99, "It means result verification registry.", NOW)
    tied_prior = _record(100, "Please keep that meaning.", NOW + timedelta(minutes=1))
    trigger = _record(
        101,
        "@bot record that",
        NOW + timedelta(minutes=1),
        reply_to=80,
    )
    records = (
        trigger,
        _record(102, "This arrived after the mention.", trigger.created_at),
        _record(98, "Another topic must not leak.", NOW, thread_id=8),
        _record(97, "Main chat must not leak.", NOW, thread_id=None),
        _record(96, "Another group must not leak.", NOW, group="other"),
        tied_prior,
        ordinary_prior,
        chain_parent,
    )

    context = ConversationContextBuilder(
        PrivacyFilter.from_yaml(ROOT / "config/privacy.yml")
    ).build(
        trigger=trigger,
        records=records,
        message_thread_id=7,
        max_chars=64_000,
        max_prior_records=100,
    )

    assert context.record_ids == (
        "tg:tawg:80",
        "tg:tawg:99",
        "tg:tawg:100",
        "tg:tawg:101",
    )
    assert "This arrived after the mention" not in context.text
    assert "Another topic" not in context.text
    assert "Main chat" not in context.text
    assert "Another group" not in context.text
    assert context.trigger_record_id == "tg:tawg:101"
    assert context.omitted_items == 0


def test_reply_chain_cannot_cross_telegram_topic_boundary() -> None:
    cross_topic_parent = _record(
        200,
        "Private context from another topic.",
        NOW,
        thread_id=8,
    )
    trigger = _record(
        201,
        "@bot summarize this topic",
        NOW + timedelta(minutes=1),
        thread_id=7,
        reply_to=200,
    )

    context = ConversationContextBuilder(
        PrivacyFilter.from_yaml(ROOT / "config/privacy.yml")
    ).build(
        trigger=trigger,
        records=(cross_topic_parent, trigger),
        message_thread_id=7,
        max_chars=64_000,
        max_prior_records=100,
    )

    assert context.record_ids == ("tg:tawg:201",)
    assert "Private context from another topic" not in context.text


def test_routing_context_sanitizes_internal_telegram_ids() -> None:
    trigger = _record(
        3513,
        "Good morning!🌞 Catching up!",
        NOW,
    ).model_copy(
        update={
            "source_payload": {
                "message_kind": "group_message",
                "message_thread_id": None,
                "update_id": 998_810_840,
            }
        }
    )

    context = ConversationContextBuilder(
        PrivacyFilter.from_yaml(ROOT / "config/privacy.yml")
    ).build(
        trigger=trigger,
        records=(trigger,),
        message_thread_id=None,
        max_chars=64_000,
        max_prior_records=100,
    )

    assert "Good morning!🌞 Catching up!" in context.text
    assert "update_id" not in context.text
    assert "998810840" not in context.text
    assert "message_thread_id" not in context.text
