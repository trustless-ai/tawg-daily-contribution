from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.bot_router import ReplyRejected
from tawg_bot.models import PendingBotJob
from tawg_bot.persist_mode import PersistMode
from tawg_bot.runtime import _safe_reason

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def test_safe_reason_collapses_whitespace() -> None:
    assert _safe_reason("reply   failed:\n\n  bad\tstate") == "reply failed: bad state"


def test_safe_reason_truncates_to_200_chars() -> None:
    assert _safe_reason("z" * 400) == "z" * 200


@pytest.mark.asyncio
async def test_reply_prepare_logs_the_bounded_rejection_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = PendingBotJob(
        job_id="reply:tg:tawg:10",
        trigger_record_id="tg:tawg:10",
        reply_to_message_id=10,
        created_at=NOW,
        updated_at=NOW,
    )

    class ReplyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def prepare(self, job_id: str, *, now: datetime) -> Any:
            del job_id, now
            raise ReplyRejected(
                "reply  job\nfailed: " + "x" * 250,
                safe_code="reply_prepare_failed",
            )

    class Reconciler:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def reconcile(self, *, now: datetime) -> None:
            del now

    monkeypatch.setattr(runtime_module, "BotReplyService", ReplyService)
    monkeypatch.setattr(runtime_module, "ReplyRepairReconciler", Reconciler)
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.delenv("TAWG_TELEGRAM_CHAT_ID", raising=False)

    pipeline = SimpleNamespace(
        root=tmp_path,
        now=NOW,
        ai=None,
        live_evidence=None,
        knowledge_state=None,
        scan_target_verifier=None,
        github_announcements=None,
        invinoveritas_client=None,
        invinoveritas_api_key=None,
        entry_candidates_enabled=False,
        member_introductions_enabled=False,
        bot_id=None,
        persist_mode=PersistMode.FULL,
        knowledge_attempted=False,
        daily_attempted=False,
        prepared_daily=None,
        prepared_replies=[],
        reply_failures=[],
        _load_jobs=lambda: [job],
    )

    await runtime_module._LivePipeline._prepare_pending_replies(pipeline)

    expected_reason = ("reply job failed: " + "x" * 250)[:200]
    assert (
        "tawg_event=phase_failed phase=reply_prepare code=reply_prepare_failed "
        f"reason={expected_reason}"
    ) in capsys.readouterr().out
    assert pipeline.reply_failures == ["reply:tg:tawg:10:reply_prepare_failed"]
