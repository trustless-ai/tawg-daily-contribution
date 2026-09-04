import hashlib
import json
from pathlib import Path

import pytest

from tawg_bot.privacy import PrivacyFilter
from tawg_bot.telegram_webhook import (
    TelegramWebhookConfig,
    TelegramWebhookNormalizer,
    is_valid_telegram_webhook_secret,
)


@pytest.mark.parametrize(
    ("secret", "valid"),
    [
        ("", False),
        ("x", False),
        ("x" * 31, False),
        ("x" * 32, True),
        ("A0_-" * 64, True),
        ("A0_-" * 64 + "x", False),
        ("x" * 32 + "!", False),
        ("x" * 31 + "秘", False),
    ],
)
def test_webhook_secret_validation_matches_telegram_contract(secret: str, valid: bool) -> None:
    assert is_valid_telegram_webhook_secret(secret) is valid

ROOT = Path(__file__).parents[2]
VALID_WEBHOOK_SECRET = "x" * 32


@pytest.mark.parametrize("secret", ["", "x", "x" * 31, "x" * 32 + "!", "x" * 31 + "秘"])
def test_webhook_config_rejects_invalid_secret(secret: str) -> None:
    with pytest.raises(ValueError, match="invalid Telegram webhook secret configuration"):
        TelegramWebhookConfig(
            secret_token=secret,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        )


def test_group_message_dispatches_a_minimal_sanitized_envelope() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 701,
            "message": {
                "message_id": 42,
                "date": 1_788_000_000,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "from": {"id": 987_654_321, "username": "alice", "first_name": "Alice"},
                "text": "hello team",
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.reason_code is None
    assert decision.envelope is not None
    assert decision.envelope.model_dump(exclude={"integrity_digest"}) == {
        "schema_version": "tawg.telegram-webhook-envelope.v1",
        "update_id": 701,
        "source_id": "tg:tawg:42",
        "message_id": 42,
        "timestamp": 1_788_000_000,
        "edited": False,
        "edited_timestamp": None,
        "text": "hello team",
        "public_username": "alice",
        "display_name": "Alice",
        "author_is_bot": False,
        "reply_to_message_id": None,
        "reply_to_message_text": None,
        "message_thread_id": None,
        "entities": (),
        "has_bot_command": False,
        "attachments": (),
        "triggers_reply": False,
    }
    digest_payload = decision.envelope.model_dump(exclude={"integrity_digest"}, mode="json")
    assert decision.envelope.integrity_digest == hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_edited_message_dispatches_with_its_edit_timestamp() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 702,
            "edited_message": {
                "message_id": 42,
                "date": 1_788_000_000,
                "edit_date": 1_788_000_100,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "from": {"id": 987_654_321, "first_name": "Alice"},
                "text": "corrected message",
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.source_id == "tg:tawg:42"
    assert decision.envelope.edited is True
    assert decision.envelope.timestamp == 1_788_000_000
    assert decision.envelope.edited_timestamp == 1_788_000_100


@pytest.mark.parametrize(
    ("caption", "entity_type", "offset", "length", "triggers_reply"),
    [
        ("🙂 @tawg_bot please help", "mention", 3, 9, True),
        ("🙂 /ask@tawg_bot please help", "bot_command", 3, 13, False),
    ],
)
def test_caption_media_preserves_safe_metadata_and_utf16_reply_trigger(
    caption: str,
    entity_type: str,
    offset: int,
    length: int,
    triggers_reply: bool,
) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 703,
            "message": {
                "message_id": 43,
                "date": 1_788_000_001,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "from": {"id": 987_654_321, "first_name": "Alice"},
                "caption": caption,
                "caption_entities": [{"type": entity_type, "offset": offset, "length": length}],
                "photo": [{"file_id": "private-file-reference"}],
                "reply_to_message": {"message_id": 41, "text": "discard me"},
                "message_thread_id": 13,
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.text == caption
    assert decision.envelope.entities[0].model_dump() == {
        "entity_type": entity_type,
        "offset": offset,
        "length": length,
        "value": "@tawg_bot" if entity_type == "mention" else "/ask@tawg_bot",
    }
    assert decision.envelope.has_bot_command is (entity_type == "bot_command")
    assert decision.envelope.attachments[0].model_dump() == {
        "media_type": "photo",
        "has_caption": True,
    }
    assert decision.envelope.reply_to_message_id == 41
    assert decision.envelope.reply_to_message_text == "discard me"
    assert decision.envelope.message_thread_id == 13
    assert decision.envelope.triggers_reply is triggers_reply


@pytest.mark.parametrize(
    ("update", "reason_code"),
    [
        (
            {
                "update_id": 704,
                "message": {
                    "message_id": 44,
                    "date": 1_788_000_002,
                    "chat": {"id": -100_999_999, "type": "supergroup"},
                    "text": "wrong group",
                },
            },
            "unexpected_chat",
        ),
        (
            {
                "update_id": 705,
                "message": {
                    "message_id": 45,
                    "date": 1_788_000_003,
                    "chat": {"id": -100_123_456, "type": "private"},
                    "text": "private chat",
                },
            },
            "unexpected_chat",
        ),
        ({"update_id": 706, "callback_query": {"id": "discard-me"}}, "unsupported_update"),
    ],
)
def test_non_group_and_unsupported_updates_are_ignored(
    update: dict[str, object], reason_code: str
) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )

    decision = normalizer.process(VALID_WEBHOOK_SECRET, json.dumps(update).encode())

    assert decision.disposition == "ignore"
    assert decision.reason_code == reason_code
    assert decision.envelope is None


def test_serialized_envelope_discards_numeric_chat_and_sender_identities() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 707,
            "message": {
                "message_id": 46,
                "date": 1_788_000_004,
                "chat": {"id": -100_123_456, "type": "supergroup", "title": "discard"},
                "from": {"id": 987_654_321, "first_name": "Alice"},
                "text": "hello",
                "document": {"file_id": "discard-me", "file_path": "/private/file"},
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.envelope is not None
    serialized = decision.envelope.model_dump_json()
    assert "-100123456" not in serialized
    assert "987654321" not in serialized
    assert "discard-me" not in serialized
    assert "/private/file" not in serialized


def test_privacy_rejected_content_has_no_sensitive_failure_detail() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 708,
            "message": {
                "message_id": 47,
                "date": 1_788_000_005,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": "sk-abcdefghijklmnopqrstuvwxyz1234567890",
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == "privacy_rejected"
    assert decision.envelope is None
    assert "abcdefghijklmnopqrstuvwxyz" not in decision.model_dump_json()


@pytest.mark.parametrize("secret_header", [None, "wrong-secret"])
def test_missing_or_wrong_secret_rejects_before_parsing(secret_header: str | None) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )

    decision = normalizer.process(secret_header, b"this is not JSON")

    assert decision.disposition == "reject"
    assert decision.reason_code == "authentication_failed"
    assert decision.envelope is None


def test_malformed_update_id_is_rejected() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": True,
            "message": {
                "message_id": 48,
                "date": 1_788_000_006,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": "hello",
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == "malformed_update"


@pytest.mark.parametrize(
    ("body", "reason_code"),
    [
        (b" " * (256 * 1024 + 1), "body_too_large"),
        (
            json.dumps(
                {
                    "update_id": 709,
                    "message": {
                        "message_id": 49,
                        "date": 1_788_000_007,
                        "chat": {"id": -100_123_456, "type": "supergroup"},
                        "text": "x" * 4097,
                    },
                }
            ).encode(),
            "text_too_large",
        ),
        (
            json.dumps(
                {
                    "update_id": 710,
                    "message": {
                        "message_id": 50,
                        "date": 1_788_000_008,
                        "chat": {"id": -100_123_456, "type": "supergroup"},
                        "text": "photo",
                        "photo": [{"file_id": f"private-{index}"} for index in range(9)],
                    },
                }
            ).encode(),
            "too_many_attachments",
        ),
    ],
    ids=("body", "text", "attachments"),
)
def test_oversized_input_is_rejected_with_a_safe_reason(body: bytes, reason_code: str) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == reason_code
    assert decision.envelope is None


def test_more_than_one_hundred_safe_entities_is_rejected() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    text = " ".join("@tawg_bot" for _ in range(101))
    body = json.dumps(
        {
            "update_id": 711,
            "message": {
                "message_id": 51,
                "date": 1_788_000_009,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": text,
                "entities": [
                    {"type": "mention", "offset": index * 10, "length": 9}
                    for index in range(101)
                ],
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == "too_many_entities"
    assert decision.envelope is None


def test_privacy_rejected_public_username_is_dropped_before_dispatch() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 712,
            "message": {
                "message_id": 52,
                "date": 1_788_000_010,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "from": {
                    "id": 987_654_321,
                    "username": "sk-abcdefghijklmnopqrstuvwxyz1234567890",
                    "first_name": "Alice",
                },
                "text": "hello",
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.public_username is None
    assert "abcdefghijklmnopqrstuvwxyz" not in decision.envelope.model_dump_json()


@pytest.mark.parametrize(
    "message",
    [
        {
            "message_id": 53,
            "date": 1_788_000_011,
            "chat": {"id": -100_123_456, "type": "supergroup"},
            "text": "hello",
            "entities": [{}],
        },
        {
            "message_id": -1,
            "date": 1_788_000_011,
            "chat": {"id": -100_123_456, "type": "supergroup"},
            "text": "hello",
        },
        {
            "message_id": 54,
            "date": 1_788_000_011,
            "chat": {"id": -100_123_456, "type": "supergroup"},
            "from": {"first_name": "a" * 257},
            "text": "hello",
        },
        {
            "message_id": 55,
            "date": 1_788_000_011,
            "chat": {"id": -100_123_456, "type": "supergroup"},
            "text": "hello",
            "entities": [{"type": "mention", "offset": 99, "length": 9}],
        },
    ],
    ids=(
        "malformed_entity",
        "negative_message_id",
        "oversized_display_name",
        "out_of_range_entity",
    ),
)
def test_malformed_update_fields_return_a_safe_rejection(message: dict[str, object]) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps({"update_id": 713, "message": message}).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == "malformed_update"
    assert decision.envelope is None


def test_more_than_one_hundred_raw_entities_is_rejected_before_filtering() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 714,
            "message": {
                "message_id": 56,
                "date": 1_788_000_012,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": "hello",
                "entities": [
                    {"type": "url", "offset": 0, "length": 1} for _ in range(101)
                ],
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "reject"
    assert decision.reason_code == "too_many_entities"
    assert decision.envelope is None


def test_redacted_text_omits_raw_offset_entity_metadata() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 715,
            "message": {
                "message_id": 57,
                "date": 1_788_000_013,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": "email alice@example.com @tawg_bot",
                "entities": [{"type": "mention", "offset": 24, "length": 9}],
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.text == "email [REDACTED_EMAIL] @tawg_bot"
    assert decision.envelope.entities == ()
    assert decision.envelope.triggers_reply is False


def test_recursively_nested_json_returns_a_safe_rejection() -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )

    decision = normalizer.process(VALID_WEBHOOK_SECRET, b"[" * 10_000 + b"]" * 10_000)

    assert decision.disposition == "reject"
    assert decision.reason_code == "malformed_update"
    assert decision.envelope is None


@pytest.mark.parametrize(
    ("text", "offset", "length"),
    [
        ("/ask", 0, 4),
        ("/ask@tawg_bot", 0, 13),
        ("/ask@other_bot", 0, 14),
        ("/hello", 0, 6),
        ("The receipt is live-pinned on /ledger for review.", 30, 7),
        ("Clients POST /observations after verification.", 13, 13),
    ],
)
def test_bot_commands_never_trigger_a_reply(text: str, offset: int, length: int) -> None:
    normalizer = TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=VALID_WEBHOOK_SECRET,
            chat_id=-100_123_456,
            group_slug="tawg",
            bot_username="tawg_bot",
        ),
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
    )
    body = json.dumps(
        {
            "update_id": 716,
            "message": {
                "message_id": 58,
                "date": 1_788_000_014,
                "chat": {"id": -100_123_456, "type": "supergroup"},
                "text": text,
                "entities": [
                    {"type": "bot_command", "offset": offset, "length": length}
                ],
            },
        }
    ).encode()

    decision = normalizer.process(VALID_WEBHOOK_SECRET, body)

    assert decision.disposition == "dispatch"
    assert decision.envelope is not None
    assert decision.envelope.has_bot_command is True
    assert decision.envelope.triggers_reply is False
