from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    RouteContextScope,
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


def test_member_welcome_jobs_are_independent_topic_messages() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    direct = PendingBotJob(
        job_id="member-welcome:logan",
        trigger_record_id="tg:tawg:81",
        reply_to_message_id=None,
        message_thread_id=77,
        trigger_kind=TriggerKind.MEMBER_WELCOME,
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:80",
        created_at=now,
        updated_at=now,
    )
    introduction = PendingBotJob(
        job_id="member-introduction:logan",
        trigger_record_id="tg:tawg:81",
        reply_to_message_id=None,
        message_thread_id=77,
        trigger_kind=TriggerKind.MEMBER_INTRODUCTION,
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:80",
        prerequisite_job_id=direct.job_id,
        created_at=now,
        updated_at=now,
    )

    assert direct.reply_to_message_id is None
    assert introduction.prerequisite_job_id == direct.job_id


def test_member_welcome_metadata_is_rejected_on_an_ordinary_reply_job() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="member welcome metadata"):
        PendingBotJob(
            job_id="reply:tg:tawg:81",
            trigger_record_id="tg:tawg:81",
            reply_to_message_id=81,
            trigger_kind=TriggerKind.GREETING_CANDIDATE,
            welcome_target_person_id="logan",
            welcome_target_record_id="tg:tawg:80",
            created_at=now,
            updated_at=now,
        )


def test_ordinary_reply_job_requires_a_reply_target() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="ordinary reply"):
        PendingBotJob(
            job_id="reply:tg:tawg:81",
            trigger_record_id="tg:tawg:81",
            reply_to_message_id=None,
            trigger_kind=TriggerKind.MENTION,
            created_at=now,
            updated_at=now,
        )


@pytest.mark.parametrize(
    "paths",
    [
        ["../knowledge/topics/rvr.md"],
        ["knowledge/topics/../meta/rvr.md"],
        ["knowledge/topics/rvr.json"],
        ["knowledge/topics/rvr.md", "knowledge/topics/rvr.md"],
    ],
)
def test_pending_reply_rejects_unconfined_knowledge_mutation_paths(
    paths: list[str],
) -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="knowledge mutation path"):
        PendingBotJob(
            job_id="reply:tg:tawg:42",
            trigger_record_id="tg:tawg:42",
            reply_to_message_id=42,
            knowledge_mutation_paths=paths,
            knowledge_mutation_trigger_sha256="a" * 64,
            status=JobStatus.DELIVERED,
            classified_route=BotRoute.KNOWLEDGE_CORRECTION,
            router_context_scope=RouteContextScope.KNOWLEDGE,
            router_context_sha256="b" * 64,
            router_version="contextual-ai-v5",
            routed_at=now,
            created_at=now,
            updated_at=now,
        )


@pytest.mark.parametrize(
    "path",
    [
        f"knowledge/topics/{'a' * 493}.md",
        "knowledge/topics/line\nbreak.md",
    ],
)
def test_pending_reply_rejects_oversized_or_controlled_mutation_path(
    path: str,
) -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="knowledge mutation path"):
        PendingBotJob(
            job_id="reply:tg:tawg:42",
            trigger_record_id="tg:tawg:42",
            reply_to_message_id=42,
            status=JobStatus.DELIVERED,
            classified_route=BotRoute.KNOWLEDGE_CORRECTION,
            router_context_scope=RouteContextScope.KNOWLEDGE,
            router_context_sha256="a" * 64,
            router_version="contextual-ai-v5",
            routed_at=now,
            knowledge_mutation_paths=[path],
            knowledge_mutation_trigger_sha256="b" * 64,
            created_at=now,
            updated_at=now,
        )


def test_pending_reply_rejects_mutation_receipt_without_completed_correction_route() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)

    with pytest.raises(ValidationError, match="knowledge mutation receipt"):
        PendingBotJob(
            job_id="reply:tg:tawg:42",
            trigger_record_id="tg:tawg:42",
            reply_to_message_id=42,
            status=JobStatus.DELIVERED,
            classified_route=BotRoute.KNOWLEDGE_QUESTION,
            router_context_scope=RouteContextScope.KNOWLEDGE,
            router_context_sha256="a" * 64,
            router_version="contextual-ai-v5",
            routed_at=now,
            knowledge_mutation_paths=["knowledge/topics/rvr.md"],
            knowledge_mutation_trigger_sha256="b" * 64,
            created_at=now,
            updated_at=now,
        )


def test_pending_reply_persistence_omits_empty_mutation_outcome() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    without_mutation = PendingBotJob(
        job_id="reply:tg:tawg:42",
        trigger_record_id="tg:tawg:42",
        reply_to_message_id=42,
        created_at=now,
        updated_at=now,
    )
    with_mutation = PendingBotJob(
        job_id="reply:tg:tawg:43",
        trigger_record_id="tg:tawg:43",
        reply_to_message_id=43,
        status=JobStatus.READY,
        classified_route=BotRoute.KNOWLEDGE_CORRECTION,
        router_context_scope=RouteContextScope.KNOWLEDGE,
        router_context_sha256="a" * 64,
        router_version="contextual-ai-v5",
        routed_at=now,
        knowledge_mutation_paths=["knowledge/topics/rvr.md"],
        knowledge_mutation_trigger_sha256="b" * 64,
        created_at=now,
        updated_at=now,
    )

    assert "knowledge_mutation_paths" not in without_mutation.persistence_payload()
    assert "knowledge_mutation_trigger_sha256" not in without_mutation.persistence_payload()
    assert with_mutation.persistence_payload()["knowledge_mutation_paths"] == [
        "knowledge/topics/rvr.md"
    ]
    assert with_mutation.persistence_payload()["knowledge_mutation_trigger_sha256"] == (
        "b" * 64
    )


def test_pending_reply_persistence_revalidates_copied_mutation_receipt() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    job = PendingBotJob(
        job_id="reply:tg:tawg:42",
        trigger_record_id="tg:tawg:42",
        reply_to_message_id=42,
        created_at=now,
        updated_at=now,
    ).model_copy(
        update={
            "knowledge_mutation_paths": [
                "knowledge/topics/one.md",
                "knowledge/topics/two.md",
                "knowledge/topics/three.md",
                "knowledge/topics/four.md",
            ]
        }
    )

    with pytest.raises(ValidationError):
        job.persistence_payload()
