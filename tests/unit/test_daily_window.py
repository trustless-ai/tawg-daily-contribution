from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from tawg_bot.daily import DailyService, DailyWindow

ROOT = Path(__file__).parents[2]


def test_delayed_run_keeps_scheduled_window() -> None:
    run_at = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)

    window = DailyWindow.for_due_run(run_at)

    assert window.start == datetime(2026, 8, 22, 23, tzinfo=UTC)
    assert window.end == datetime(2026, 8, 23, 23, tzinfo=UTC)
    assert window.window_id == "daily:2026-08-23T23:00:00Z"


def test_window_is_start_inclusive_and_end_exclusive() -> None:
    window = DailyWindow.for_due_run(datetime(2026, 8, 23, 23, tzinfo=UTC))

    assert window.contains(datetime(2026, 8, 22, 23, tzinfo=UTC))
    assert window.contains(datetime(2026, 8, 23, 22, 59, 59, tzinfo=UTC))
    assert not window.contains(datetime(2026, 8, 23, 23, tzinfo=UTC))


def test_window_rejects_non_utc_run_time() -> None:
    with pytest.raises(ValueError, match="UTC"):
        DailyWindow.for_due_run(datetime(2026, 8, 24, 1, 17))


def test_daily_keeps_legacy_two_part_content_in_one_rich_message() -> None:
    service = DailyService(ROOT, ai=cast(Any, object()))
    text = "a" * 3_000 + "\n\n" + "b" * 900 + "\n" + "c" * 3_800

    messages = service._split(text)

    assert messages == (text,)


def test_daily_splits_content_above_the_rich_message_limit_at_a_paragraph() -> None:
    service = DailyService(ROOT, ai=cast(Any, object()))
    text = "a" * 32_000 + "\n\n" + "b" * 2_000

    messages = service._split(text)

    assert len(messages) == 2
    assert messages[0] == "a" * 32_000
    assert messages[1] == "b" * 2_000
    assert all(len(message) <= 32_768 for message in messages)
