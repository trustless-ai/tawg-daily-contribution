from __future__ import annotations

from pathlib import Path

from tawg_bot.models import TelegramWebhookReceipts
from tawg_bot.persist_mode import PersistMode
from tawg_bot.runtime_env import resolve_env

_STATE_DIR = "data/state"
_LEGACY_FILENAME = "telegram-webhook-receipts.json"
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
    token = resolve_env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return bot_id_from_token(token)


def webhook_receipt_relative_path(bot_id: int | None) -> str:
    if bot_id is None:
        return f"{_STATE_DIR}/{_LEGACY_FILENAME}"
    return f"{_STATE_DIR}/telegram-webhook-receipts.{bot_id}.json"


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
