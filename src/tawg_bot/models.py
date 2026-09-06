"""Versioned domain models shared by ingestion, knowledge, and delivery."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_text(value: str) -> str:
    """Normalize source text without changing its language or meaning."""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


class StrictModel(BaseModel):
    """Base model that rejects undeclared data."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceType(StrEnum):
    TELEGRAM_MESSAGE = "telegram_message"
    GITHUB_FILE = "github_file"
    GITHUB_COMMIT = "github_commit"
    GITHUB_ISSUE = "github_issue"
    GITHUB_PULL_REQUEST = "github_pull_request"
    GITHUB_COMMENT = "github_comment"
    GITHUB_REVIEW = "github_review"
    GITHUB_DISCUSSION = "github_discussion"
    GITHUB_RELEASE = "github_release"
    MAGICIANS_POST = "magicians_post"


class Relation(StrictModel):
    relation_type: str = Field(min_length=1, max_length=64)
    target_record_id: str = Field(min_length=1, max_length=256)


class AttachmentMetadata(StrictModel):
    media_type: str = Field(min_length=1, max_length=32)
    has_caption: bool = False
    interpreted: bool = False


class SourceRecord(StrictModel):
    schema_version: str = "tawg.source-record.v1"
    record_id: str = Field(min_length=1, max_length=256)
    source_type: SourceType
    source_locator: str = Field(min_length=1, max_length=2048)
    author_person_id: str | None = Field(default=None, max_length=128)
    author_source_handle: str | None = Field(default=None, max_length=256)
    created_at: datetime
    updated_at: datetime
    text_original: str
    english_summary: str | None = None
    relations: list[Relation] = Field(default_factory=list)
    attachment_metadata: list[AttachmentMetadata] = Field(default_factory=list)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ingested_at: datetime
    source_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", "ingested_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("text_original")
    @classmethod
    def text_is_normalized(cls, value: str) -> str:
        return normalize_text(value)

    @model_validator(mode="after")
    def updated_after_creation(self) -> SourceRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        expected = hashlib.sha256(self.text_original.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match text_original")
        return self

    @classmethod
    def from_text(
        cls,
        *,
        record_id: str,
        source_type: SourceType,
        source_locator: str,
        author_person_id: str | None,
        author_source_handle: str | None,
        created_at: datetime,
        updated_at: datetime,
        text_original: str,
        ingested_at: datetime,
        english_summary: str | None = None,
        relations: list[Relation] | None = None,
        attachment_metadata: list[AttachmentMetadata] | None = None,
        source_payload: dict[str, Any] | None = None,
    ) -> SourceRecord:
        normalized = normalize_text(text_original)
        return cls(
            record_id=record_id,
            source_type=source_type,
            source_locator=source_locator,
            author_person_id=author_person_id,
            author_source_handle=author_source_handle,
            created_at=created_at,
            updated_at=updated_at,
            text_original=normalized,
            english_summary=english_summary,
            relations=relations or [],
            attachment_metadata=attachment_metadata or [],
            content_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            ingested_at=ingested_at,
            source_payload=source_payload or {},
        )


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    DELIVERED = "delivered"
    IGNORED = "ignored"
    CANCELLED = "cancelled"


class TriggerKind(StrEnum):
    MENTION = "mention"
    REPLY_TO_BOT = "reply_to_bot"
    GREETING_CANDIDATE = "greeting_candidate"
    MEMBER_WELCOME = "member_welcome"
    MEMBER_INTRODUCTION = "member_introduction"


class BotRoute(StrEnum):
    KNOWLEDGE_QUESTION = "knowledge_question"
    IDENTITY_CORRECTION = "identity_correction"
    KNOWLEDGE_CORRECTION = "knowledge_correction"
    SOURCE_SUGGESTION = "source_suggestion"
    COORDINATION = "coordination"
    VERIFICATION = "verification"
    REFUSE = "refuse"
    IGNORE = "ignore"


class RouteContextScope(StrEnum):
    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    ERC = "erc"


class PreparedAttachment(StrictModel):
    """A file the controller should deliver alongside a reply as a Telegram document."""

    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_000_000)


class PendingBotJob(StrictModel):
    schema_version: str = "tawg.pending-bot-job.v1"
    job_id: str
    trigger_record_id: str
    reply_to_message_id: int | None
    message_thread_id: int | None = None
    trigger_kind: TriggerKind = TriggerKind.MENTION
    status: JobStatus = JobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    prepared_reply_text: str | None = Field(default=None, max_length=10_500)
    prepared_attachments: list[PreparedAttachment] = Field(default_factory=list, max_length=8)
    prepared_citations: list[str] = Field(default_factory=list)
    prepared_language: str | None = Field(default=None, max_length=32)
    refusal: bool = False
    safe_error_code: str | None = Field(default=None, max_length=64)
    classified_route: BotRoute | None = None
    router_context_scope: RouteContextScope | None = None
    router_context_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    router_version: str | None = Field(default=None, max_length=64)
    routed_at: datetime | None = None
    verification_artifact: str | None = Field(default=None, max_length=4096)
    repair_of_job_id: str | None = Field(default=None, max_length=128)
    repair_reason_code: str | None = Field(default=None, max_length=64)
    welcome_target_person_id: str | None = Field(default=None, max_length=128)
    welcome_target_record_id: str | None = Field(default=None, max_length=256)
    prerequisite_job_id: str | None = Field(default=None, max_length=256)
    knowledge_mutation_paths: list[str] = Field(default_factory=list, max_length=3)
    knowledge_mutation_trigger_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime

    _created_at_utc = field_validator("created_at")(_require_utc)
    _updated_at_utc = field_validator("updated_at")(_require_utc)
    _routed_at_utc = field_validator("routed_at")(
        lambda value: None if value is None else _require_utc(value)
    )

    @field_validator("knowledge_mutation_paths")
    @classmethod
    def knowledge_paths_are_confined(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("knowledge mutation paths must be unique")
        for value in values:
            path = PurePosixPath(value)
            if (
                len(value) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or "\\" in value
                or path.is_absolute()
                or path.as_posix() != value
                or ".." in path.parts
                or (len(path.parts) < 3 and path.parts != ("knowledge", "index.md"))
                or path.parts[0] != "knowledge"
                or path.suffix.casefold() != ".md"
            ):
                raise ValueError("invalid knowledge mutation path")
        return values

    def persistence_payload(self) -> dict[str, Any]:
        validated = type(self).model_validate(self.model_dump(mode="json"))
        exclude = (
            {"knowledge_mutation_paths", "knowledge_mutation_trigger_sha256"}
            if not validated.knowledge_mutation_paths
            else None
        )
        return validated.model_dump(mode="json", exclude=exclude)

    @model_validator(mode="after")
    def repair_metadata_is_complete(self) -> PendingBotJob:
        if (self.repair_of_job_id is None) != (self.repair_reason_code is None):
            raise ValueError("reply repair metadata must be complete")
        if self.repair_of_job_id == self.job_id:
            raise ValueError("reply repair cannot supersede itself")
        member_kinds = {
            TriggerKind.MEMBER_WELCOME,
            TriggerKind.MEMBER_INTRODUCTION,
        }
        has_member_metadata = any(
            value is not None
            for value in (
                self.welcome_target_person_id,
                self.welcome_target_record_id,
                self.prerequisite_job_id,
            )
        )
        if self.trigger_kind in member_kinds:
            if (
                self.reply_to_message_id is not None
                or self.welcome_target_person_id is None
                or self.welcome_target_record_id is None
                or (
                    self.trigger_kind is TriggerKind.MEMBER_WELCOME
                    and self.prerequisite_job_id is not None
                )
                or (
                    self.trigger_kind is TriggerKind.MEMBER_INTRODUCTION
                    and self.prerequisite_job_id is None
                )
            ):
                raise ValueError("member welcome metadata is incomplete")
        elif has_member_metadata or self.reply_to_message_id is None:
            raise ValueError("member welcome metadata is not allowed on an ordinary reply")
        routing = (
            self.classified_route,
            self.router_context_sha256,
            self.router_version,
            self.routed_at,
        )
        if any(value is not None for value in routing) and any(value is None for value in routing):
            raise ValueError("reply routing metadata must be complete")
        has_mutation_paths = bool(self.knowledge_mutation_paths)
        has_mutation_hash = self.knowledge_mutation_trigger_sha256 is not None
        if has_mutation_paths != has_mutation_hash:
            raise ValueError("knowledge mutation receipt must be complete")
        member_welcome_mutation = (
            self.trigger_kind is TriggerKind.MEMBER_WELCOME
            and self.classified_route is BotRoute.COORDINATION
        )
        if has_mutation_paths and (
            self.status not in {JobStatus.READY, JobStatus.DELIVERED}
            or (
                self.classified_route is not BotRoute.KNOWLEDGE_CORRECTION
                and not member_welcome_mutation
            )
            or self.router_context_scope is None
        ):
            raise ValueError("knowledge mutation receipt requires a completed correction route")
        if self.status is JobStatus.IGNORED and (
            self.trigger_kind is not TriggerKind.GREETING_CANDIDATE
            or self.classified_route is not BotRoute.IGNORE
            or self.prepared_reply_text is not None
            or self.prepared_language is not None
            or self.prepared_citations
        ):
            raise ValueError(
                "ignored reply state must be a greeting candidate with no deliverable content"
            )
        return self


class SourceCursors(StrictModel):
    schema_version: str = "tawg.source-cursors.v1"
    telegram_offset: int = Field(default=0, ge=0)
    github: dict[str, str | int | None] = Field(default_factory=dict)
    magicians: dict[str, str | int | None] = Field(default_factory=dict)
    knowledge_record_id: str | None = None
    knowledge_updated_at: datetime | None = None

    @field_validator("knowledge_updated_at")
    @classmethod
    def knowledge_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class TelegramWebhookReceipts(StrictModel):
    schema_version: Literal["tawg.telegram-webhook-receipts.v1"]
    update_ids: list[Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=list)

    @field_validator("update_ids")
    @classmethod
    def retain_highest_distinct_update_ids(cls, value: list[int]) -> list[int]:
        return sorted(set(value))[-2048:]


class LayerSuccess(StrictModel):
    schema_version: str = "tawg.layer-success.v1"
    l1: datetime | None = None
    l2: datetime | None = None
    l3: datetime | None = None
    l4: datetime | None = None

    @field_validator("l1", "l2", "l3", "l4")
    @classmethod
    def layer_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class DeliveryStatus(StrEnum):
    PREPARED = "prepared"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class DeliveryAttempt(StrictModel):
    schema_version: str = "tawg.delivery-attempt.v1"
    delivery_id: str
    job_id: str
    destination: str = "tawg"
    status: DeliveryStatus = DeliveryStatus.PREPARED
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reply_text: str | None = Field(default=None, max_length=70_000)
    message_count: int = Field(default=0, ge=0, le=2)
    delivery_format: str | None = Field(
        default=None,
        pattern=(
            r"^(rich_markdown_v1|rich_html_fallback_v1|"
            r"plain_text_fallback_v1|mixed_v1)$"
        ),
    )
    reply_to_message_id: int | None = None
    message_thread_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_ids: list[int] = Field(default_factory=list)
    sent_at: datetime | None = None
    prepared_at: datetime
    updated_at: datetime
    safe_error_code: str | None = None

    _prepared_at_utc = field_validator("prepared_at")(_require_utc)
    _delivery_updated_at_utc = field_validator("updated_at")(_require_utc)

    @field_validator("sent_at")
    @classmethod
    def sent_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class RejectedRecord(StrictModel):
    schema_version: str = "tawg.rejected-record.v1"
    source_id: str
    rejected_at: datetime
    reason_code: str = Field(min_length=1, max_length=64)

    _rejected_at_utc = field_validator("rejected_at")(_require_utc)
