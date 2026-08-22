from datetime import UTC, datetime

import pytest

from tawg_bot.daily import DailyWindow


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

