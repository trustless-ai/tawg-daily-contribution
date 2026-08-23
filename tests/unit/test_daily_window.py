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


def test_daily_split_chooses_a_boundary_that_keeps_both_messages_within_limit() -> None:
    service = DailyService(ROOT, ai=cast(Any, object()))
    text = "a" * 3_000 + "\n\n" + "b" * 900 + "\n" + "c" * 3_800

    messages = service._split(text)

    assert len(messages) == 2
    assert all(len(message) <= 4_096 for message in messages)
