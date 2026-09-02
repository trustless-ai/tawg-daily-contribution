import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tawg_bot.privacy import PrivacyFilter, PrivacyViolation, PrivateChatRejected

ROOT = Path(__file__).parents[2]


@pytest.fixture
def redactor() -> PrivacyFilter:
    return PrivacyFilter.from_yaml(ROOT / "config/privacy.yml")


def test_privacy_cases_are_deterministic(redactor: PrivacyFilter) -> None:
    fixture = yaml.safe_load((ROOT / "tests/fixtures/privacy_cases.yml").read_text())
    for case in fixture["cases"]:
        result = redactor.inspect(case["input"])
        assert result.accepted is case["accepted"], case["name"]
        if case["accepted"]:
            assert result.sanitized_text == case["output"], case["name"]
        else:
            assert result.reason_code == case["reason"], case["name"]


def test_dash_delimited_unix_timestamp_is_not_redacted_as_phone(
    redactor: PrivacyFilter,
) -> None:
    """A Nostr proof `d` tag carries a 10-digit unix timestamp bounded by dashes (not JSON
    punctuation). It must stay intact, not be misread as a phone number."""
    value = (
        "invinoveritas-proof-"
        "e0b0dc557bb89124b8edfe53e86ad08b42ae28004746f9cf801206d527cafe24"
        "-1786889259-5ec66c8f"
    )
    result = redactor.inspect(value)
    assert result.accepted is True
    assert result.sanitized_text == value


def test_rejected_text_is_not_copied_into_failure_record(redactor: PrivacyFilter) -> None:
    source = "seed phrase: alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    result = redactor.inspect(source)

    assert result.accepted is False
    assert result.reason_code == "secret_material"
    assert "alpha" not in result.safe_failure_json(source_id="tg:tawg:42")


def test_payload_removes_numeric_user_ids_but_keeps_message_id(
    redactor: PrivacyFilter,
) -> None:
    sanitized = redactor.sanitize_payload(
        {
            "message_id": 42,
            "from_id": 987654321,
            "from": {"id": 987654321, "username": "alice"},
            "text": "hello @alice",
        }
    )

    assert sanitized == {
        "message_id": 42,
        "from": {"username": "alice"},
        "text": "hello @alice",
    }
    assert "987654321" not in json.dumps(sanitized)


def test_internal_metadata_stripping_recurses_lists_without_mutating_input(
    redactor: PrivacyFilter,
) -> None:
    payload = {
        "messages": [
            {
                "text_original": "Good morning!",
                "source_payload": {
                    "update_id": 998_810_840,
                    "message_thread_id": None,
                },
            }
        ]
    }
    original = deepcopy(payload)

    stripped = redactor.strip_internal_metadata(payload)

    assert stripped == {
        "messages": [
            {
                "text_original": "Good morning!",
                "source_payload": {},
            }
        ]
    }
    assert payload == original


def test_private_chat_payload_fails_closed(redactor: PrivacyFilter) -> None:
    with pytest.raises(PrivateChatRejected):
        redactor.sanitize_payload({"chat": {"type": "private"}, "text": "hello"})


def test_iso_dates_are_not_mistaken_for_phone_numbers(redactor: PrivacyFilter) -> None:
    result = redactor.inspect("created: 2026-08-23")

    assert result.accepted
    assert result.sanitized_text == "created: 2026-08-23"


@pytest.mark.parametrize(
    "text",
    [
        "Updated through post #394 (2026-08-24).",
        "Updated through 2026-08-24 (394 posts).",
    ],
)
def test_forum_post_number_next_to_iso_date_is_not_a_phone(
    redactor: PrivacyFilter,
    text: str,
) -> None:
    result = redactor.inspect(text)

    assert result.accepted
    assert result.sanitized_text == text


def test_phone_number_next_to_iso_date_is_still_redacted(
    redactor: PrivacyFilter,
) -> None:
    result = redactor.inspect("Contact 4155550123 (2026-08-24 follow-up).")

    assert result.accepted
    assert result.sanitized_text is not None
    assert "4155550123" not in result.sanitized_text
    assert "[REDACTED_PHONE]" in result.sanitized_text


@pytest.mark.parametrize(
    ("text", "phone"),
    [
        (
            "Updated through post #394 (2026-08-24); call 4155550123.",
            "4155550123",
        ),
        (
            "Call 415-555-0123 after post #394 (2026-08-24).",
            "415-555-0123",
        ),
        (
            "post #394 dated 2026-08-24; phone +1 (415) 555-0123",
            "+1 (415) 555-0123",
        ),
    ],
)
def test_dated_forum_post_does_not_exempt_nearby_phone(
    redactor: PrivacyFilter,
    text: str,
    phone: str,
) -> None:
    result = redactor.inspect(text)

    assert result.accepted
    assert result.sanitized_text is not None
    assert phone not in result.sanitized_text
    assert "[REDACTED_PHONE]" in result.sanitized_text


def test_long_decimal_requires_explicit_technical_context(
    redactor: PrivacyFilter,
) -> None:
    result = redactor.inspect("Account 1234567890123456")

    assert result.accepted
    assert result.sanitized_text == "Account [REDACTED_PHONE]"


@pytest.mark.parametrize("field", ["observed_sha256", "content_sha256"])
def test_decimal_sha256_fixture_is_explicit_technical_context(
    redactor: PrivacyFilter,
    field: str,
) -> None:
    text = f'"{field}":"' + "0" * 64 + '"'

    result = redactor.inspect(text)

    assert result.accepted
    assert result.sanitized_text == text


def test_structured_sha256_value_requires_sha256_field(
    redactor: PrivacyFilter,
) -> None:
    value = "0" * 64

    redactor.assert_public_value(value, parent_key="observed_sha256")
    with pytest.raises(PrivacyViolation, match="unredacted_personal_data"):
        redactor.assert_public_value(value, parent_key="account_number")


@pytest.mark.parametrize(
    "text",
    [
        "Account hash=1234567890123456",
        '"hash":"1234567890123456"',
        "not-int 1234567890123456",
        "uint999 1234567890123456",
        "not-int" + " " * 61 + "1234567890123456",
        "accountuint256" + " " * 57 + "1234567890123456",
        "账户int 1234567890123456",
        "éuint256 1234567890123456",
        "\N{GREEK SMALL LETTER ALPHA}int8 1234567890123456",
    ],
)
def test_untrusted_labels_do_not_exempt_long_account_numbers(
    redactor: PrivacyFilter,
    text: str,
) -> None:
    result = redactor.inspect(text)

    assert result.accepted
    assert result.sanitized_text is not None
    assert "1234567890123456" not in result.sanitized_text
    assert "[REDACTED_PHONE]" in result.sanitized_text


@pytest.mark.parametrize("integer_type", ["int", "uint", "int8", "uint256"])
def test_legal_integer_type_exempts_long_decimal(
    redactor: PrivacyFilter,
    integer_type: str,
) -> None:
    text = f"{integer_type} 123456789012345678901234567890"

    assert redactor.inspect(text).sanitized_text == text


def test_github_comment_record_id_is_not_treated_as_a_phone(
    redactor: PrivacyFilter,
) -> None:
    record_id = "gh:agent-ercs:issue:17:comment:5379076880"

    assert redactor.inspect(record_id).sanitized_text == record_id
    assert redactor.inspect("call 5379076880").sanitized_text == "call [REDACTED_PHONE]"
    assert redactor.inspect("reach me at gh:phone:5379076880").sanitized_text == (
        "reach me at gh:phone:[REDACTED_PHONE]"
    )


def test_knowledge_refresh_operation_id_does_not_allow_phone_shaped_suffixes(
    redactor: PrivacyFilter,
) -> None:
    safe_operation_id = "knowledge-refresh-20260825t000000z"

    assert redactor.inspect(safe_operation_id).sanitized_text == safe_operation_id
    assert redactor.inspect("knowledge-refresh-1415555012").sanitized_text == (
        "knowledge-refresh-[REDACTED_PHONE]"
    )


@pytest.mark.parametrize(
    "credential",
    [
        "AK" + "IA" + "A" * 16,
        "xo" + "xb-" + "1" * 12 + "-" + "a" * 24,
    ],
)
def test_common_provider_credentials_are_rejected(
    redactor: PrivacyFilter,
    credential: str,
) -> None:
    result = redactor.inspect(f"credential: {credential}")

    assert not result.accepted
    assert result.reason_code == "secret_material"
