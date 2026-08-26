"""Deterministic Telegram text splitting shared by preparation and delivery."""

from __future__ import annotations

import re


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

    protected_ranges = _fenced_code_ranges(text)
    for separator in ("\n\n", "\n"):
        candidates = [
            match.end()
            for match in re.finditer(re.escape(separator), text)
            if match.end() <= limit
            and not _inside_protected_range(match.end(), protected_ranges)
        ]
        for split_at in reversed(candidates):
            messages = (text[:split_at].rstrip(), text[split_at:].lstrip())
            if all(message and len(message) <= limit for message in messages):
                return messages
    raise TelegramTextSplitError("text has no safe Telegram message boundary")


def _fenced_code_ranges(text: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    open_at: int | None = None
    marker_char: str | None = None
    marker_size = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker is not None:
            token = marker.group(1)
            if open_at is None:
                open_at = offset
                marker_char = token[0]
                marker_size = len(token)
            elif (
                token[0] == marker_char
                and len(token) >= marker_size
                and not stripped[marker.end() :].strip()
            ):
                ranges.append((open_at, offset + len(line)))
                open_at = None
                marker_char = None
                marker_size = 0
        offset += len(line)
    if open_at is not None:
        ranges.append((open_at, len(text)))
    return tuple(ranges)


def _inside_protected_range(position: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start < position < end for start, end in ranges)
