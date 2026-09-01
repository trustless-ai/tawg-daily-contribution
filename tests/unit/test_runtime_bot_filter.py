from datetime import UTC, datetime

from tawg_bot.models import PendingBotJob
from tawg_bot.persist_mode import PersistMode
from tawg_bot.runtime import _filter_bot_local_jobs

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _job(job_id: str) -> PendingBotJob:
    return PendingBotJob(
        job_id=job_id,
        trigger_record_id="tg:tawg:1",
        reply_to_message_id=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_receipt_only_keeps_only_current_bot_jobs():
    jobs = [
        _job("reply:88:tg:tawg:1"),
        _job("reply:tg:tawg:2"),
        _job("reply:88:tg:tawg:3"),
    ]
    filtered = _filter_bot_local_jobs(
        jobs, bot_id=88, persist_mode=PersistMode.RECEIPT_ONLY
    )
    assert [j.job_id for j in filtered] == [
        "reply:88:tg:tawg:1",
        "reply:88:tg:tawg:3",
    ]


def test_full_mode_keeps_all_jobs():
    jobs = [_job("reply:88:tg:tawg:1"), _job("reply:tg:tawg:2")]
    filtered = _filter_bot_local_jobs(jobs, bot_id=88, persist_mode=PersistMode.FULL)
    assert [j.job_id for j in filtered] == ["reply:88:tg:tawg:1", "reply:tg:tawg:2"]


def test_receipt_only_without_bot_id_keeps_all():
    jobs = [_job("reply:88:tg:tawg:1"), _job("reply:tg:tawg:2")]
    filtered = _filter_bot_local_jobs(
        jobs, bot_id=None, persist_mode=PersistMode.RECEIPT_ONLY
    )
    assert [j.job_id for j in filtered] == ["reply:88:tg:tawg:1", "reply:tg:tawg:2"]
