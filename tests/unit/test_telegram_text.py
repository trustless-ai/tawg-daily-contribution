import pytest

from tawg_bot.telegram_text import TelegramTextSplitError, split_telegram_text


def test_split_rejects_a_boundary_inside_a_fenced_code_block() -> None:
    text = "A" * 20 + "\n\n```\n" + ("code line\n" * 5) + "```\n\n" + "B" * 25

    with pytest.raises(TelegramTextSplitError):
        split_telegram_text(text, limit=60, max_messages=2)


def test_fence_marker_with_trailing_text_does_not_close_the_code_block() -> None:
    text = (
        "A" * 10
        + "\n\n```\n"
        + ("code\n" * 4)
        + "```still code\n"
        + ("more\n" * 5)
        + "```\n\n"
        + "B" * 25
    )

    with pytest.raises(TelegramTextSplitError):
        split_telegram_text(text, limit=60, max_messages=2)
