"""Deterministic Telegram text splitting shared by preparation and delivery."""

from __future__ import annotations


class TelegramTextSplitError(ValueError):
    """Raised when text cannot fit the configured Telegram message budget."""


def split_telegram_text(text: str, *, limit: int, max_messages: int) -> tuple[str, ...]:
    if limit <= 0 or max_messages not in {1, 2}:
        raise ValueError("Telegram split limits are invalid")
    if not text.strip():
        raise TelegramTextSplitError("Telegram text cannot be empty")
    if len(text) <= limit:
        return (text,)
    if max_messages == 1 or len(text) > limit * max_messages:
        raise TelegramTextSplitError("text cannot fit in the Telegram message budget")

    minimum_split = max(len(text) - limit, 1)
    split_at = text.rfind("\n\n", minimum_split, limit + 1)
    if split_at < minimum_split:
        split_at = text.rfind("\n", minimum_split, limit + 1)
    if split_at < minimum_split:
        split_at = limit
    messages = (text[:split_at].rstrip(), text[split_at:].lstrip())
    if any(not message or len(message) > limit for message in messages):
        raise TelegramTextSplitError("text cannot fit in the Telegram message budget")
    return messages
