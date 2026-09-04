"""Privacy-safe normalization of Telegram webhook updates."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from tawg_bot.models import StrictModel
from tawg_bot.privacy import PrivacyFilter

MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_LENGTH = 4_096
MAX_SAFE_ENTITIES = 100
MAX_SAFE_ATTACHMENTS = 8
_TELEGRAM_WEBHOOK_SECRET_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


def is_valid_telegram_webhook_secret(secret: str) -> bool:
    """Return whether a secret satisfies Telegram's webhook token contract."""
    return 32 <= len(secret) <= 256 and all(
        character in _TELEGRAM_WEBHOOK_SECRET_CHARS for character in secret
    )


class _FrozenStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelegramWebhookDisposition(StrEnum):
    DISPATCH = "dispatch"
    IGNORE = "ignore"
    REJECT = "reject"


class TelegramWebhookEntity(_FrozenStrictModel):
    entity_type: str = Field(min_length=1, max_length=32)
    offset: int = Field(ge=0)
    length: int = Field(gt=0)
    value: str = Field(max_length=4096)


class TelegramWebhookAttachment(_FrozenStrictModel):
    media_type: str = Field(min_length=1, max_length=32)
    has_caption: bool = False


class TelegramWebhookEnvelope(_FrozenStrictModel):
    schema_version: str = "tawg.telegram-webhook-envelope.v1"
    update_id: int = Field(ge=0)
    source_id: str = Field(min_length=1, max_length=256)
    message_id: int = Field(ge=0)
    timestamp: int = Field(ge=0)
    edited: bool
    edited_timestamp: int | None = Field(default=None, ge=0)
    text: str = Field(max_length=4096)
    public_username: str | None = Field(default=None, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    author_is_bot: bool = False
    reply_to_message_id: int | None = Field(default=None, ge=0)
    reply_to_message_text: str | None = Field(default=None, max_length=4096)
    message_thread_id: int | None = Field(default=None, ge=0)
    entities: tuple[TelegramWebhookEntity, ...] = ()
    has_bot_command: bool = False
    attachments: tuple[TelegramWebhookAttachment, ...] = ()
    triggers_reply: bool
    integrity_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class TelegramWebhookDecision(_FrozenStrictModel):
    disposition: TelegramWebhookDisposition
    reason_code: str | None = Field(default=None, max_length=64)
    envelope: TelegramWebhookEnvelope | None = None


def telegram_entities_trigger_reply(
    entities: tuple[TelegramWebhookEntity, ...], *, bot_username: str
) -> bool:
    """Classify a reply trigger from sanitized Telegram entity values."""
    normalized_bot_username = bot_username.casefold().lstrip("@")
    for entity in entities:
        value = entity.value.casefold()
        if entity.entity_type == "mention" and value == f"@{normalized_bot_username}":
            return True
    return False


@dataclass(frozen=True, slots=True)
class TelegramWebhookConfig:
    secret_token: str
    chat_id: int
    group_slug: str
    bot_username: str

    def __post_init__(self) -> None:
        if not is_valid_telegram_webhook_secret(self.secret_token):
            raise ValueError("invalid Telegram webhook secret configuration")


class TelegramWebhookNormalizer:
    """Convert one authenticated Telegram update into a safe durable input."""

    def __init__(self, *, config: TelegramWebhookConfig, privacy: PrivacyFilter) -> None:
        self._config = config
        self._privacy = privacy

    def process(self, secret_header: str | None, body: bytes) -> TelegramWebhookDecision:
        if secret_header is None or not hmac.compare_digest(
            secret_header, self._config.secret_token
        ):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code="authentication_failed",
            )
        if len(body) > MAX_BODY_BYTES:
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code="body_too_large",
            )
        try:
            update = json.loads(body)
        except (RecursionError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code="malformed_update",
            )
        if not isinstance(update, dict):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code="malformed_update",
            )
        message = update.get("message")
        edited = False
        if not isinstance(message, dict):
            message = update.get("edited_message")
            edited = True
        if not isinstance(message, dict):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.IGNORE,
                reason_code="unsupported_update",
            )
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code="malformed_update",
            )
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        if (
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id != self._config.chat_id
            or chat_type not in {"group", "supergroup"}
        ):
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.IGNORE,
                reason_code="unexpected_chat",
            )
        envelope, reason_code = self._envelope(update, message, edited=edited)
        if envelope is None:
            return TelegramWebhookDecision(
                disposition=TelegramWebhookDisposition.REJECT,
                reason_code=reason_code or "malformed_update",
            )
        return TelegramWebhookDecision(
            disposition=TelegramWebhookDisposition.DISPATCH,
            envelope=envelope,
        )

    def _envelope(
        self, update: dict[str, Any], message: dict[str, Any], *, edited: bool
    ) -> tuple[TelegramWebhookEnvelope | None, str | None]:
        update_id = update.get("update_id")
        message_id = message.get("message_id")
        timestamp = message.get("date")
        edited_timestamp = message.get("edit_date") if edited else None
        is_caption = "caption" in message and "text" not in message
        text = message.get("caption") if is_caption else message.get("text", "")
        if (
            not isinstance(update_id, int)
            or isinstance(update_id, bool)
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id < 0
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or timestamp < 0
            or (
                edited
                and (
                    not isinstance(edited_timestamp, int) or isinstance(edited_timestamp, bool)
                )
            )
            or (edited and isinstance(edited_timestamp, int) and edited_timestamp < 0)
            or not isinstance(text, str)
        ):
            return None, "malformed_update"
        if len(text) > MAX_TEXT_LENGTH:
            return None, "text_too_large"
        text_result = self._privacy.inspect(text)
        if not text_result.accepted or text_result.sanitized_text is None:
            return None, "privacy_rejected"
        author = message.get("from")
        author_mapping = author if isinstance(author, dict) else {}
        username = author_mapping.get("username")
        username_result = self._privacy.inspect(username) if isinstance(username, str) else None
        public_username = (
            username_result.sanitized_text
            if username_result is not None
            and username_result.accepted
            and username_result.sanitized_text is not None
            else None
        )
        display_name = " ".join(
            value
            for value in (author_mapping.get("first_name"), author_mapping.get("last_name"))
            if isinstance(value, str)
        ).strip()
        display_result = self._privacy.inspect(display_name or "Unknown member")
        if not display_result.accepted or not display_result.sanitized_text:
            return None, "privacy_rejected"
        entities, entity_reason = self._entities(
            text,
            message.get("caption_entities") if is_caption else message.get("entities"),
        )
        if entities is None:
            return None, entity_reason or "malformed_update"
        has_bot_command = any(entity.entity_type == "bot_command" for entity in entities)
        if text_result.sanitized_text != text:
            entities = ()
        reply = message.get("reply_to_message")
        reply_to_message_id = (
            reply.get("message_id")
            if isinstance(reply, dict)
            and isinstance(reply.get("message_id"), int)
            and not isinstance(reply.get("message_id"), bool)
            else None
        )
        reply_to_message_text = None
        if isinstance(reply, dict):
            raw_reply_text = reply.get("text") or reply.get("caption")
            if isinstance(raw_reply_text, str):
                reply_to_message_text = raw_reply_text
        message_thread_id = message.get("message_thread_id")
        if not isinstance(message_thread_id, int) or isinstance(message_thread_id, bool):
            message_thread_id = None
        if self._attachment_count(message) > MAX_SAFE_ATTACHMENTS:
            return None, "too_many_attachments"
        attachments = self._attachments(message, has_caption=is_caption)
        payload = {
            "schema_version": "tawg.telegram-webhook-envelope.v1",
            "update_id": update_id,
            "source_id": f"tg:{self._config.group_slug}:{message_id}",
            "message_id": message_id,
            "timestamp": timestamp,
            "edited": edited,
            "edited_timestamp": edited_timestamp,
            "text": text_result.sanitized_text,
            "public_username": public_username,
            "display_name": display_result.sanitized_text,
            "author_is_bot": author_mapping.get("is_bot") is True,
            "reply_to_message_id": reply_to_message_id,
            "reply_to_message_text": reply_to_message_text,
            "message_thread_id": message_thread_id,
            "entities": entities,
            "has_bot_command": has_bot_command,
            "attachments": attachments,
            "triggers_reply": telegram_entities_trigger_reply(
                entities,
                bot_username=self._config.bot_username,
            ),
        }
        try:
            envelope = TelegramWebhookEnvelope(**payload, integrity_digest="0" * 64)
        except ValidationError:
            return None, "malformed_update"
        return (
            envelope.model_copy(update={"integrity_digest": self._integrity_digest(envelope)}),
            None,
        )

    @staticmethod
    def _integrity_digest(envelope: TelegramWebhookEnvelope) -> str:
        payload = envelope.model_dump(exclude={"integrity_digest"}, mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _entities(
        self, text: str, raw_entities: object
    ) -> tuple[tuple[TelegramWebhookEntity, ...] | None, str | None]:
        if raw_entities is None:
            return (), None
        if not isinstance(raw_entities, list):
            return None, "malformed_update"
        if len(raw_entities) > MAX_SAFE_ENTITIES:
            return None, "too_many_entities"
        entities: list[TelegramWebhookEntity] = []
        for raw_entity in raw_entities:
            if not isinstance(raw_entity, dict):
                return None, "malformed_update"
            entity_type = raw_entity.get("type")
            offset = raw_entity.get("offset")
            length = raw_entity.get("length")
            if (
                not isinstance(entity_type, str)
                or not isinstance(offset, int)
                or isinstance(offset, bool)
                or not isinstance(length, int)
                or isinstance(length, bool)
                or offset < 0
                or length <= 0
            ):
                return None, "malformed_update"
            if entity_type not in {"mention", "bot_command"}:
                continue
            if offset + length > len(text.encode("utf-16-le")) // 2:
                return None, "malformed_update"
            try:
                value = self._utf16_slice(text, offset, length)
            except UnicodeDecodeError:
                return None, "malformed_update"
            entities.append(
                TelegramWebhookEntity(
                    entity_type=entity_type,
                    offset=offset,
                    length=length,
                    value=value,
                )
            )
            if len(entities) > MAX_SAFE_ENTITIES:
                return None, "too_many_entities"
        return tuple(entities), None

    @staticmethod
    def _utf16_slice(text: str, offset: int, length: int) -> str:
        encoded = text.encode("utf-16-le")
        return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")

    @staticmethod
    def _attachments(
        message: dict[str, Any], *, has_caption: bool
    ) -> tuple[TelegramWebhookAttachment, ...]:
        for field, media_type in (
            ("photo", "photo"),
            ("video", "video"),
            ("video_note", "video"),
            ("document", "file"),
            ("audio", "audio"),
            ("voice", "audio"),
            ("animation", "animation"),
            ("sticker", "sticker"),
        ):
            if field in message:
                return (TelegramWebhookAttachment(media_type=media_type, has_caption=has_caption),)
        return ()

    @staticmethod
    def _attachment_count(message: dict[str, Any]) -> int:
        count = 0
        for field in (
            "photo",
            "video",
            "video_note",
            "document",
            "audio",
            "voice",
            "animation",
            "sticker",
        ):
            value = message.get(field)
            if isinstance(value, list):
                count += len(value)
            elif value is not None:
                count += 1
        return count
