from __future__ import annotations

import os
from pathlib import Path

from tawg_bot.models import TelegramWebhookReceipts
from tawg_bot.persist_mode import PersistMode

_STATE_DIR = "data/state"
_LEGACY_FILENAME = "telegram-webhook-receipts.json"
_DELIVERY_FILENAME = "delivery-state.json"
_SCHEMA = "tawg.telegram-webhook-receipts.v1"


def bot_id_from_token(token: str) -> int:
    prefix = token.split(":", 1)[0]
    try:
        bot_id = int(prefix)
    except ValueError:
        raise ValueError("TELEGRAM_BOT_TOKEN must begin with a numeric bot id") from None
    if bot_id <= 0:
        raise ValueError("TELEGRAM_BOT_TOKEN bot id must be positive")
    return bot_id


def configured_bot_id() -> int | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return bot_id_from_token(token)


def webhook_receipt_relative_path(bot_id: int | None) -> str:
    if bot_id is None:
        return f"{_STATE_DIR}/{_LEGACY_FILENAME}"
    return f"{_STATE_DIR}/telegram-webhook-receipts.{bot_id}.json"


def delivery_state_relative_path(bot_id: int | None, *, persist_mode: PersistMode) -> str:
    """Return the delivery-state path for this worker.

    Receipt-only workers (the dev mirror) keep their delivered message ids in a
    bot-id-scoped file so they never overwrite the authoritative main worker's
    ``delivery-state.json``, while still letting the intake layer recognise a
    reply to one of the mirror bot's own messages as ``REPLY_TO_BOT``.
    """
    if persist_mode is PersistMode.RECEIPT_ONLY and bot_id is not None:
        return f"{_STATE_DIR}/delivery-state.{bot_id}.json"
    return f"{_STATE_DIR}/{_DELIVERY_FILENAME}"

def load_webhook_receipts(
    root: Path,
    *,
    bot_id: int | None,
    persist_mode: PersistMode = PersistMode.FULL,
) -> TelegramWebhookReceipts:
    path = root / webhook_receipt_relative_path(bot_id)
    if path.exists():
        return TelegramWebhookReceipts.model_validate_json(path.read_text(encoding="utf-8"))
    if bot_id is not None and persist_mode is PersistMode.FULL:
        legacy = root / webhook_receipt_relative_path(None)
        if legacy.exists():
            return TelegramWebhookReceipts.model_validate_json(legacy.read_text(encoding="utf-8"))
    return TelegramWebhookReceipts(schema_version=_SCHEMA)
