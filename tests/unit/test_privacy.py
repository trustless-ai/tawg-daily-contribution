import json
from pathlib import Path

import pytest
import yaml

from tawg_bot.privacy import PrivacyFilter, PrivateChatRejected

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


def test_private_chat_payload_fails_closed(redactor: PrivacyFilter) -> None:
    with pytest.raises(PrivateChatRejected):
        redactor.sanitize_payload({"chat": {"type": "private"}, "text": "hello"})


def test_iso_dates_are_not_mistaken_for_phone_numbers(redactor: PrivacyFilter) -> None:
    result = redactor.inspect("created: 2026-08-23")

    assert result.accepted
    assert result.sanitized_text == "created: 2026-08-23"
