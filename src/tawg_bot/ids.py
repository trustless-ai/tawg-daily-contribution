"""Stable public identifiers for source records."""

import re

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _validated_segment(value: object, *, name: str) -> str:
    segment = str(value).strip()
    if not segment or not _SEGMENT.fullmatch(segment):
        raise ValueError(f"{name} must contain only letters, numbers, dot, underscore, or dash")
    return segment


def telegram_id(group_slug: str, message_id: int) -> str:
    """Return the stable ID for a message in the configured Telegram group."""
    if message_id <= 0:
        raise ValueError("message_id must be positive")
    return f"tg:{_validated_segment(group_slug, name='group_slug').lower()}:{message_id}"


def github_id(repository: str, *coordinates: object) -> str:
    """Return the stable ID for an object in a GitHub repository."""
    if not coordinates:
        raise ValueError("at least one GitHub coordinate is required")
    parts = [_validated_segment(repository, name="repository").lower()]
    parts.extend(_validated_segment(value, name="coordinate") for value in coordinates)
    return "gh:" + ":".join(parts)


def magicians_id(topic_id: int, post_id: int) -> str:
    """Return the stable ID for an Ethereum Magicians post."""
    if topic_id <= 0 or post_id <= 0:
        raise ValueError("topic_id and post_id must be positive")
    return f"magicians:{topic_id}:post:{post_id}"
