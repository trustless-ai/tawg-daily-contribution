from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    SourceRecord,
    SourceType,
    TriggerKind,
)


def test_source_record_computes_content_hash_from_normalized_text() -> None:
    record = SourceRecord.from_text(
        record_id="tg:tawg:42",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:42",
        author_person_id="jimmy",
        author_source_handle="jimmy",
        created_at=datetime(2026, 8, 22, 23, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, 23, tzinfo=UTC),
        text_original="Shipping the parser today.",
        ingested_at=datetime(2026, 8, 23, tzinfo=UTC),
    )

    assert record.content_sha256 == (
        "aa75f61d28fc6a40dc0245ac28468cdc496718ff35b5cfb76cf94826324c5a57"
    )


def test_source_record_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        SourceRecord.from_text(
            record_id="tg:tawg:42",
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:42",
            author_person_id="jimmy",
            author_source_handle="jimmy",
            created_at=datetime(2026, 8, 22, 23),
            updated_at=datetime(2026, 8, 22, 23),
            text_original="text",
            ingested_at=datetime(2026, 8, 23, tzinfo=UTC),
        )


def test_only_a_greeting_candidate_can_have_ignored_reply_state() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="greeting candidate"):
        PendingBotJob(
            job_id="reply:tg:tawg:42",
            trigger_record_id="tg:tawg:42",
            reply_to_message_id=42,
            trigger_kind=TriggerKind.REPLY_TO_BOT,
            status=JobStatus.IGNORED,
            classified_route=BotRoute.IGNORE,
            router_context_sha256="a" * 64,
            router_version="contextual-ai-v2",
            routed_at=now,
            created_at=now,
            updated_at=now,
        )
