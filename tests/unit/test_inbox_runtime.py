from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.bot_router import PreparedReply
from tawg_bot.feature_flags import ENTRY_ROUTER_VERSION
from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    RouteContextScope,
    TriggerKind,
)
from tawg_bot.persist_mode import PersistMode
from tawg_bot.repository_session import (
    CommandResult,
    RepositoryConflict,
    RepositorySession,
)
from tawg_bot.runtime import InboxDrainReport, ProductionRuntime, RuntimeFailure
from tawg_bot.telegram_inbox import (
    INBOX_MAX_ATTEMPTS,
    InboxDrainSelection,
    InboxEntry,
    InboxStatus,
    done_path,
    enqueue_envelope,
    entries,
    incoming_path,
    save_state,
    state_path,
)
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


class Checkpoint:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)


class FakeAi:
    route_entry_decision: ClassVar[str] = "ignore"
    route_entry_route: ClassVar[str] = "ignore"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if kwargs.get("job_type") == "route_entry":
            return {
                "schema_version": "tawg.route-entry-result.v1",
                "decision": self.route_entry_decision,
                "addressed_to_bot": False,
                "route": self.route_entry_route,
                "context_scope": "conversation",
            }
        return {
            "schema_version": "tawg.route-result.v2",
            "route": "coordination",
            "context_scope": "conversation",
        }


class FakePipeline:
    instances: ClassVar[list[FakePipeline]] = []
    ambiguous: ClassVar[bool] = False
    fail_delivery: ClassVar[bool] = False

    def __init__(self, root: Path, **kwargs: object) -> None:
        self.root = root
        self.checkpoint = kwargs.get("checkpoint")
        self.bot_id = kwargs.get("bot_id")
        self.persist_mode = kwargs.get("persist_mode", PersistMode.FULL)
        self.ai = FakeAi()
        self.prepared_replies: list[PreparedReply] = []
        self.publish_calls = 0
        self.delivery_delivered_job_ids: set[str] = set()
        self.delivery_ambiguous_job_ids: set[str] = set()
        self.delivery_failed_job_ids: set[str] = set()
        self.delivery_rejected_job_ids: set[str] = set()
        FakePipeline.instances.append(self)

    def _chat_id(self) -> int:
        return -100424242

    def _load_jobs(self) -> list[PendingBotJob]:
        path = self.root / "data/state/pending-bot-jobs.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return [PendingBotJob.model_validate(item) for item in raw]

    async def publish_repository(self) -> None:
        self.publish_calls += 1
        if self.checkpoint is not None:
            await self.checkpoint.publish(f"prepared:{self.publish_calls}", self.root)
        self.prepared_replies = [
            PreparedReply(
                job_id=job.job_id,
                reply_to_message_id=job.reply_to_message_id,
                message_thread_id=job.message_thread_id,
                reply_text="prepared",
                citations=(),
                language="en",
                refusal=False,
            )
            for job in self._load_jobs()
        ]

    async def telegram_delivery(self) -> None:
        if self.fail_delivery:
            self.delivery_failed_job_ids.update(
                reply.job_id for reply in self.prepared_replies
            )
        elif self.ambiguous:
            self.delivery_ambiguous_job_ids.update(
                reply.job_id for reply in self.prepared_replies
            )
        else:
            self.delivery_delivered_job_ids.update(
                reply.job_id for reply in self.prepared_replies
            )


def _envelope(
    update_id: int,
    message_id: int,
    *,
    reply_to_message_id: int | None = None,
    text: str = "ordinary message",
) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=False,
        text=text,
        display_name="Alice",
        reply_to_message_id=reply_to_message_id,
        triggers_reply=False,
        integrity_digest="0" * 64,
    )
    payload = value.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value.model_copy(update={"integrity_digest": digest})


def _job(message_id: int) -> PendingBotJob:
    return PendingBotJob(
        job_id=f"reply:tg:tawg:{message_id}",
        trigger_record_id=f"tg:tawg:{message_id}",
        reply_to_message_id=message_id,
        trigger_kind=TriggerKind.MESSAGE_CANDIDATE,
        status=JobStatus.READY,
        prepared_reply_text="prepared",
        prepared_language="en",
        classified_route=BotRoute.COORDINATION,
        router_context_scope=RouteContextScope.CONVERSATION,
        router_context_sha256="a" * 64,
        router_version=ENTRY_ROUTER_VERSION,
        routed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _state_entry(
    update_id: int,
    message_id: int,
    status: InboxStatus,
    *,
    prepared: bool = False,
) -> InboxEntry:
    envelope = _envelope(update_id, message_id)
    entry = enqueue_envelope(envelope, now=NOW, root=INBOX_ROOT, bot_key="bot")
    reply = PreparedReply(
        job_id=f"reply:tg:tawg:{message_id}",
        reply_to_message_id=message_id,
        message_thread_id=None,
        reply_text="original prepared reply",
        citations=(),
        language="en",
        refusal=False,
    )
    prepared_payload = asdict(reply) if prepared else None
    updated = entry.model_copy(
        update={
            "status": status,
            "jobs": (_job(message_id).persistence_payload(),),
            "prepared_replies": (prepared_payload,) if prepared_payload else (),
        }
    )
    save_state(updated, root=INBOX_ROOT, bot_key="bot")
    return updated


INBOX_ROOT = Path("/tmp/tawg-inbox-runtime-test")


@pytest.fixture(autouse=True)
def _inbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global INBOX_ROOT
    INBOX_ROOT = tmp_path / "inbox"
    monkeypatch.setenv("TAWG_TELEGRAM_INBOX_DIR", str(INBOX_ROOT))
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "bot")
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_MODE", "receipt-only")
    FakePipeline.instances.clear()
    FakePipeline.ambiguous = False
    FakePipeline.fail_delivery = False
    FakeAi.route_entry_decision = "ignore"
    FakeAi.route_entry_route = "ignore"
    monkeypatch.setattr(runtime_module, "_LivePipeline", FakePipeline)


@pytest.mark.asyncio
async def test_maintenance_does_not_process_undecided_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    entry = enqueue_envelope(
        _envelope(1, 1), now=NOW, root=INBOX_ROOT, bot_key="bot"
    )
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.maintenance_only is True
    assert report.backlog_seen == 1
    assert report.remaining == 1
    assert FakePipeline.instances == []
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.ENQUEUED
    assert entry.update_id == 1


@pytest.mark.asyncio
async def test_receipt_only_polling_is_disabled_on_new_clone_before_telegram_or_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_MODE", "receipt-only")
    copy_static_runtime_tree(ROOT, tmp_path)
    new_clone = tmp_path / "new-clone"
    copy_static_runtime_tree(ROOT, new_clone)
    runtime = ProductionRuntime(new_clone, checkpoint=Checkpoint())

    with pytest.raises(RuntimeFailure, match="receipt-only polling is disabled"):
        await runtime.tick(NOW, observe_only=False)

    assert FakePipeline.instances == []


@pytest.mark.asyncio
async def test_receipt_only_reply_to_own_inbox_finalizes_one_scoped_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8750877254:test-token")
    FakeAi.route_entry_decision = "reply"
    FakeAi.route_entry_route = "coordination"
    expected_job_id = "reply:8750877254:tg:tawg:31"
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    (tmp_path / "data/state/delivery-state.8750877254.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": expected_job_id,
                    "job_id": expected_job_id,
                    "destination": "tawg",
                    "status": "delivered",
                    "content_sha256": "a" * 64,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "reply_to_message_id": 11,
                    "message_thread_id": None,
                    "telegram_chat_id": -100424242,
                    "telegram_message_ids": [901],
                    "sent_at": timestamp,
                    "prepared_at": timestamp,
                    "updated_at": timestamp,
                    "safe_error_code": None,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    enqueue_envelope(
        _envelope(31, 31, reply_to_message_id=901, text="Could you clarify?"),
        now=NOW,
        root=INBOX_ROOT,
        bot_key="bot",
    )
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW, new_update_ids=(31,))

    assert report.delivered == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()
    jobs = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )
    assert [(job["job_id"], job["trigger_kind"]) for job in jobs] == [
        (expected_job_id, "reply_to_bot")
    ]
    assert FakePipeline.instances[-1].delivery_delivered_job_ids == {expected_job_id}
    assert all(
        job["job_id"] != "reply:tg:tawg:31" for job in jobs
    )


@pytest.mark.asyncio
async def test_receipt_only_new_clone_resumes_decided_delivery_without_entry_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    entry = _state_entry(4, 4, InboxStatus.ENTRY_DECIDED)

    # Simulate a new clone: only the inbox persists; the repository checkout is fresh.
    new_clone = tmp_path / "new-clone"
    copy_static_runtime_tree(ROOT, new_clone)
    (new_clone / "data/state/telegram-webhook-receipts.json").write_text(
        json.dumps(
            {
                "schema_version": "tawg.telegram-webhook-receipts.v1",
                "update_ids": [4],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = ProductionRuntime(new_clone, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.pending + report.delivered == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()
    pipeline = FakePipeline.instances[0]
    assert pipeline.ai.calls == []  # entry AI was not called
    assert pipeline.delivery_delivered_job_ids == {entry.jobs[0]["job_id"]}


@pytest.mark.asyncio
async def test_ambiguous_entry_is_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(5, 5, InboxStatus.AMBIGUOUS)
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.selected == 0
    assert FakePipeline.instances == []
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.AMBIGUOUS
    output = capsys.readouterr().out
    assert "tawg_event=inbox_alert" in output
    assert "status=ambiguous count=1" in output
    assert "reason=delivery_ambiguous" in output


@pytest.mark.asyncio
async def test_ambiguous_delivery_is_recorded_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(6, 6, InboxStatus.DELIVERY_PENDING)
    FakePipeline.ambiguous = True
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    first = await runtime.process_inbox(NOW)

    assert first.ambiguous == 1
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.AMBIGUOUS
    assert len(FakePipeline.instances) == 1

    second = await runtime.process_inbox(NOW)

    assert second.selected == 0
    assert len(FakePipeline.instances) == 1
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.AMBIGUOUS


@pytest.mark.asyncio
async def test_event_local_retries_exhaust_to_alert_and_retain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    enqueue_envelope(_envelope(9, 9), now=NOW, root=INBOX_ROOT, bot_key="bot")

    async def fail_entry(*args: object, **kwargs: object) -> InboxEntry:
        del args, kwargs
        raise RuntimeError("worker exploded")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ProductionRuntime, "_process_inbox_entry", fail_entry)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", no_sleep)
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW, new_update_ids=(9,))

    assert report.pending == 1
    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.NEEDS_ATTENTION
    assert stored.attempts == INBOX_MAX_ATTEMPTS
    output = capsys.readouterr().out
    assert "tawg_event=inbox_alert" in output
    assert "update_id=9" in output
    assert "ordinary message" not in output

    maintenance = await runtime.process_inbox(NOW)
    assert maintenance.selected == 0
    assert maintenance.needs_attention == 1
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.NEEDS_ATTENTION


@pytest.mark.asyncio
async def test_delivery_failure_exhaustion_alerts_and_stays_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(16, 16, InboxStatus.DELIVERY_PENDING, prepared=True)
    FakePipeline.fail_delivery = True
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    for _ in range(INBOX_MAX_ATTEMPTS - 1):
        report = await runtime.process_inbox(NOW)
        assert report.pending == 1
        assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is (
            InboxStatus.DELIVERY_PENDING
        )

    exhausted = await runtime.process_inbox(NOW)
    assert exhausted.pending == 1
    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.NEEDS_ATTENTION
    assert stored.attempts == INBOX_MAX_ATTEMPTS
    assert all(not pipeline.ai.calls for pipeline in FakePipeline.instances)
    output = capsys.readouterr().out
    assert "tawg_event=inbox_alert" in output
    assert "update_id=16" in output
    assert "status=needs_attention" in output
    assert "reason=retry_exhausted" in output
    assert "original prepared reply" not in output

    maintenance = await runtime.process_inbox(NOW + timedelta(minutes=5))
    assert maintenance.selected == 0
    assert maintenance.cleaned == 0
    assert maintenance.needs_attention == 1
    maintenance_output = capsys.readouterr().out
    assert "tawg_event=inbox_drain" in maintenance_output
    assert "needs_attention=1" in maintenance_output
    assert "status=needs_attention count=1" in maintenance_output
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is (
        InboxStatus.NEEDS_ATTENTION
    )
    assert len(FakePipeline.instances) == INBOX_MAX_ATTEMPTS
    assert all(not pipeline.ai.calls for pipeline in FakePipeline.instances)


@pytest.mark.asyncio
async def test_maintenance_alerts_on_stuck_undecided_without_entry_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    enqueue_envelope(_envelope(10, 10), now=NOW, root=INBOX_ROOT, bot_key="bot")
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.selected == 0
    assert FakePipeline.instances == []
    output = capsys.readouterr().out
    assert "tawg_event=inbox_alert" in output
    assert "status=undecided_pending" in output
    assert entries(root=INBOX_ROOT, bot_key="bot")[0].status is InboxStatus.ENQUEUED


@pytest.mark.asyncio
async def test_delivery_pending_restores_prepared_text_without_regeneration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(11, 11, InboxStatus.DELIVERY_PENDING, prepared=True)
    FakePipeline.fail_delivery = True
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    first = await runtime.process_inbox(NOW)

    assert first.pending == 1
    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.DELIVERY_PENDING
    assert stored.attempts == 1
    assert stored.prepared_replies[0]["reply_text"] == "original prepared reply"
    assert FakePipeline.instances[0].publish_calls == 0
    assert FakePipeline.instances[0].ai.calls == []

    FakePipeline.fail_delivery = False
    second = await runtime.process_inbox(NOW + timedelta(seconds=30))

    assert second.delivered == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()
    assert all(pipeline.publish_calls == 0 for pipeline in FakePipeline.instances)
    assert all(not pipeline.ai.calls for pipeline in FakePipeline.instances)


@pytest.mark.asyncio
async def test_terminal_delivery_state_finalizes_after_receipt_without_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    entry = _state_entry(12, 12, InboxStatus.ENTRY_DECIDED)
    job_id = str(entry.jobs[0]["job_id"])
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": job_id,
                    "job_id": job_id,
                    "destination": "tawg",
                    "status": "delivered",
                    "content_sha256": "a" * 64,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "reply_to_message_id": 12,
                    "message_thread_id": None,
                    "telegram_chat_id": -100424242,
                    "telegram_message_ids": [900],
                    "sent_at": timestamp,
                    "prepared_at": timestamp,
                    "updated_at": timestamp,
                    "safe_error_code": None,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.delivered == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()
    assert FakePipeline.instances == []


@pytest.mark.asyncio
async def test_duplicate_selection_and_tombstone_do_not_double_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    entry = _state_entry(13, 13, InboxStatus.DELIVERY_PENDING, prepared=True)
    selection = InboxDrainSelection(
        selected=(entry, entry),
        new_event_updates=(),
        backlog_seen=1,
        backlog_selected=0,
        backlog_remaining=0,
        oldest_undecided_age_seconds=0,
        maintenance_only=True,
        retry_selected=0,
        needs_attention=0,
    )
    monkeypatch.setattr(
        runtime_module, "select_drain_entries", lambda *args, **kwargs: selection
    )
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.delivered == 1
    assert len(FakePipeline.instances) == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()


@pytest.mark.asyncio
async def test_exception_after_decision_save_reads_latest_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    enqueue_envelope(_envelope(14, 14), now=NOW, root=INBOX_ROOT, bot_key="bot")
    seen_statuses: list[InboxStatus] = []

    async def fail_after_decision(
        self: ProductionRuntime, entry: InboxEntry, now: datetime, **kwargs: object
    ) -> InboxEntry:
        del self, now, kwargs
        seen_statuses.append(entry.status)
        state = entry.model_copy(
            update={
                "status": InboxStatus.ENTRY_DECIDED,
                "jobs": (_job(14).persistence_payload(),),
                "attempts": entry.attempts,
            }
        )
        save_state(state, root=INBOX_ROOT, bot_key="bot")
        raise RuntimeError("after decision save")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ProductionRuntime, "_process_inbox_entry", fail_after_decision)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", no_sleep)
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW, new_update_ids=(14,))

    assert report.pending == 1
    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.NEEDS_ATTENTION
    assert stored.jobs == (_job(14).persistence_payload(),)
    assert seen_statuses[0] is InboxStatus.ENQUEUED
    assert all(status is InboxStatus.ENTRY_DECIDED for status in seen_statuses[1:])
    assert FakePipeline.instances == []


@pytest.mark.asyncio
async def test_exception_after_prepared_save_preserves_reply_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    enqueue_envelope(_envelope(15, 15), now=NOW, root=INBOX_ROOT, bot_key="bot")
    prepared = (
        {
            "job_id": "reply:tg:tawg:15",
            "reply_to_message_id": 15,
            "message_thread_id": None,
            "reply_text": "original prepared reply",
            "citations": (),
            "language": "en",
            "refusal": False,
            "attachments": (),
        },
    )
    seen_statuses: list[InboxStatus] = []

    async def fail_after_prepared(
        self: ProductionRuntime, entry: InboxEntry, now: datetime, **kwargs: object
    ) -> InboxEntry:
        del self, now, kwargs
        seen_statuses.append(entry.status)
        state = entry.model_copy(
            update={
                "status": InboxStatus.DELIVERY_PENDING,
                "jobs": (_job(15).persistence_payload(),),
                "prepared_replies": prepared,
                "attempts": entry.attempts,
            }
        )
        save_state(state, root=INBOX_ROOT, bot_key="bot")
        raise RuntimeError("after prepared save")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ProductionRuntime, "_process_inbox_entry", fail_after_prepared)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", no_sleep)
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW, new_update_ids=(15,))

    assert report.pending == 1
    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.NEEDS_ATTENTION
    assert stored.prepared_replies[0]["reply_text"] == "original prepared reply"
    assert seen_statuses[0] is InboxStatus.ENQUEUED
    assert all(status is InboxStatus.DELIVERY_PENDING for status in seen_statuses[1:])
    assert FakePipeline.instances == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [InboxStatus.DELIVERED, InboxStatus.TERMINAL])
async def test_maintenance_cleans_terminal_entry_before_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: InboxStatus,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(20, 20, status, prepared=True)
    assert incoming_path(INBOX_ROOT, "bot", 20).exists()
    assert state_path(INBOX_ROOT, "bot", 20).exists()
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    report = await runtime.process_inbox(NOW)

    assert report.selected == 1
    assert report.cleaned == 1
    assert report.delivered == 0
    assert report.pending == 0
    assert FakePipeline.instances == []
    assert done_path(INBOX_ROOT, "bot", 20).exists()
    assert not incoming_path(INBOX_ROOT, "bot", 20).exists()
    assert not state_path(INBOX_ROOT, "bot", 20).exists()
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()

    # Cleanup is idempotent: a repeated maintenance tick is a no-op.
    repeated = await runtime.process_inbox(NOW)
    assert repeated.selected == 0
    assert repeated.cleaned == 0
    assert FakePipeline.instances == []


@pytest.mark.asyncio
async def test_maintenance_removes_files_orphaned_after_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    _state_entry(21, 21, InboxStatus.DELIVERED, prepared=True)
    done_path(INBOX_ROOT, "bot", 21).parent.mkdir(parents=True, exist_ok=True)
    done_path(INBOX_ROOT, "bot", 21).write_bytes(b"")

    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())
    report = await runtime.process_inbox(NOW)

    assert report.selected == 0
    assert report.cleaned == 1
    assert FakePipeline.instances == []
    assert not incoming_path(INBOX_ROOT, "bot", 21).exists()
    assert not state_path(INBOX_ROOT, "bot", 21).exists()
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()


@pytest.mark.asyncio
async def test_repository_conflict_escapes_inbox_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    enqueue_envelope(_envelope(22, 22), now=NOW, root=INBOX_ROOT, bot_key="bot")

    async def conflict(
        self: ProductionRuntime,
        entry: InboxEntry,
        now: datetime,
        *,
        allow_entry: bool,
        persist: object,
    ) -> InboxEntry:
        del self, entry, now, allow_entry, persist
        raise RepositoryConflict

    monkeypatch.setattr(ProductionRuntime, "_process_inbox_entry", conflict)
    runtime = ProductionRuntime(tmp_path, checkpoint=Checkpoint())

    with pytest.raises(RepositoryConflict):
        await runtime.process_inbox(NOW, new_update_ids=(22,))

    stored = entries(root=INBOX_ROOT, bot_key="bot")[0]
    assert stored.status is InboxStatus.ENQUEUED
    assert stored.attempts == 0
    assert FakePipeline.instances == []


class _RepositoryRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult:
        del cwd
        command = tuple(argv)
        self.calls.append(command)
        if command[:2] == ("git", "clone"):
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(returncode=0)


class _ConflictOnceCheckpoint:
    def __init__(self) -> None:
        self.conflicts = 1
        self.operations: list[str] = []

    async def publish(self, operation_id: str, root: Path) -> None:
        assert root.exists()
        self.operations.append(operation_id)
        if self.conflicts and operation_id.startswith("prepared:"):
            self.conflicts -= 1
            raise RepositoryConflict


@pytest.mark.asyncio
async def test_repository_conflict_replays_on_fresh_clone_without_entry_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _state_entry(23, 23, InboxStatus.ENTRY_DECIDED)
    checkpoint = _ConflictOnceCheckpoint()
    checkouts: list[Path] = []
    reports: list[InboxDrainReport] = []

    async def operation(checkout: Path) -> None:
        checkouts.append(checkout)
        copy_static_runtime_tree(ROOT, checkout)
        runtime = ProductionRuntime(checkout, checkpoint=checkpoint)
        reports.append(await runtime.process_inbox(NOW))

    session = RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="main",
        runner=_RepositoryRunner(),
    )
    await session.run(operation_id="webhook:23", operation=operation)

    assert checkpoint.conflicts == 0
    assert len(checkouts) == 2
    assert checkouts[0] != checkouts[1]
    assert all(not checkout.exists() for checkout in checkouts)
    assert len(reports) == 1
    assert reports[0].delivered == 1
    assert entries(root=INBOX_ROOT, bot_key="bot") == ()
    assert done_path(INBOX_ROOT, "bot", 23).exists()
    assert entry.jobs[0]["job_id"] in FakePipeline.instances[-1].delivery_delivered_job_ids
    assert all(
        call.get("job_type") != "route_entry"
        for pipeline in FakePipeline.instances
        for call in pipeline.ai.calls
    )
