import json
from pathlib import Path

import pytest

from tawg_bot.bot_identity import (
    bot_id_from_token,
    load_webhook_receipts,
    webhook_receipt_relative_path,
)
from tawg_bot.persist_mode import PersistMode


def test_bot_id_from_token_uses_numeric_prefix():
    assert bot_id_from_token("123456789:AA-remainder") == 123456789


def test_bot_id_from_token_rejects_non_numeric_prefix():
    with pytest.raises(ValueError):
        bot_id_from_token("not-a-number:AA")


def test_bot_id_from_token_rejects_non_positive():
    with pytest.raises(ValueError):
        bot_id_from_token("0:AA-remainder")


def test_receipt_path_namespaced_and_legacy():
    assert (
        webhook_receipt_relative_path(None)
        == "data/state/telegram-webhook-receipts.json"
    )
    assert (
        webhook_receipt_relative_path(77)
        == "data/state/telegram-webhook-receipts.77.json"
    )


def test_load_receipts_falls_back_only_in_full_mode(tmp_path: Path):
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    legacy = state / "telegram-webhook-receipts.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": "tawg.telegram-webhook-receipts.v1",
                "update_ids": [1, 2],
            }
        )
    )
    got = load_webhook_receipts(tmp_path, bot_id=5, persist_mode=PersistMode.FULL)
    assert got.update_ids == [1, 2]
    got = load_webhook_receipts(
        tmp_path, bot_id=6, persist_mode=PersistMode.RECEIPT_ONLY
    )
    assert got.update_ids == []


def test_load_receipts_prefers_namespaced_file(tmp_path: Path):
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    (state / "telegram-webhook-receipts.7.json").write_text(
        json.dumps(
            {
                "schema_version": "tawg.telegram-webhook-receipts.v1",
                "update_ids": [9],
            }
        )
    )
    got = load_webhook_receipts(tmp_path, bot_id=7, persist_mode=PersistMode.FULL)
    assert got.update_ids == [9]


def test_load_receipts_legacy_path_when_no_bot_id(tmp_path: Path):
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    legacy = state / "telegram-webhook-receipts.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": "tawg.telegram-webhook-receipts.v1",
                "update_ids": [3],
            }
        )
    )
    got = load_webhook_receipts(tmp_path, bot_id=None)
    assert got.update_ids == [3]
