from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.bot_router import BotReplyService
from tawg_bot.claude_cli import ClaudeCliError
from tawg_bot.entry_router import EntryDecider
from tawg_bot.feature_flags import ENTRY_ROUTER_VERSION, entry_candidates_enabled
from tawg_bot.models import (
    BotRoute,
    JobStatus,
    PendingBotJob,
    RouteContextScope,
    SourceRecord,
    SourceType,
    TriggerKind,
)
from tawg_bot.persist_mode import PersistMode
from tawg_bot.telegram_intake import (
    TelegramIntake,
    commit_intake_state,
    ingest_envelopes,
)
from tawg_bot.telegram_webhook import TelegramWebhookEntity, TelegramWebhookEnvelope
from tests.support.runtime_repository import copy_static_runtime_tree

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


class FakeAi:
    def __init__(
        self,
        *,
        route: str = "coordination",
        decision: str | None = None,
        addressed_to_bot: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.route = route
        self.decision = decision or ("ignore" if route == "ignore" else "reply")
        self.addressed_to_bot = addressed_to_bot
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "schema_version": "tawg.route-entry-result.v1",
            "route": self.route,
            "context_scope": "conversation",
            "decision": self.decision,
            "addressed_to_bot": self.addressed_to_bot,
        }


class ReplyFakeAi(FakeAi):
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("job_type") in {"route", "route_entry"}:
            return {
                "schema_version": "tawg.route-entry-result.v1",
                "route": self.route,
                "context_scope": "conversation",
                "decision": self.decision,
                "addressed_to_bot": self.addressed_to_bot,
            }
        return {
            "schema_version": "tawg.reply-result.v3",
            "reply_text": "Hello!",
            "language": "en",
            "english_recap": None,
            "citations": [],
            "evidence_status": "verified",
            "verification_gaps": [],
            "correction_transaction": None,
            "knowledge_write": None,
            "scan_registration": None,
            "refusal": False,
        }


def envelope(
    *,
    update_id: int,
    message_id: int,
    text: str,
    triggers_reply: bool = False,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    author_is_bot: bool = False,
    has_bot_command: bool = False,
    entities: tuple[TelegramWebhookEntity, ...] = (),
    public_username: str = "alice_tawg",
    display_name: str = "Alice",
) -> TelegramWebhookEnvelope:
    value = TelegramWebhookEnvelope(
        update_id=update_id,
        source_id=f"tg:tawg:{message_id}",
        message_id=message_id,
        timestamp=int(NOW.timestamp()),
        edited=False,
        text=text,
        public_username=public_username,
        display_name=display_name,
        author_is_bot=author_is_bot,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        entities=entities,
        has_bot_command=has_bot_command,
        triggers_reply=triggers_reply,
        integrity_digest="0" * 64,
    )
    payload = value.model_dump(exclude={"integrity_digest"}, mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value.model_copy(update={"integrity_digest": digest})


def _ingest(root: Path, item: TelegramWebhookEnvelope, *, defer: bool = True):
    return ingest_envelopes(
        root=root,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(item,),
        now=NOW,
        telegram_chat_id=-100424242,
        bot_id=None,
        persist_mode=PersistMode.FULL,
        defer_receipts=defer,
    )


def _jobs(root: Path) -> dict[str, PendingBotJob]:
    raw = json.loads((root / "data/state/pending-bot-jobs.json").read_text())
    jobs = [PendingBotJob.model_validate(job) for job in raw]
    return {job.job_id: job for job in jobs}


@pytest.mark.asyncio
async def test_webhook_receipt_is_committed_after_entry_and_survives_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(update_id=1, message_id=10, text="what is ERC-8183?")

    first = _ingest(tmp_path, item)
    assert first.candidate_job_ids == ("reply:tg:tawg:10",)
    receipt_path = tmp_path / "data/state/telegram-webhook-receipts.json"
    assert not receipt_path.exists()

    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(first.candidate_job_ids)
    assert report.ai_calls == 1
    assert len(ai.calls) == 1
    assert ai.calls[0]["job_type"] == "route_entry"
    job = _jobs(tmp_path)["reply:tg:tawg:10"]
    assert job.classified_route is not None
    assert job.routed_at is not None

    # Crash after entry, before the receipt commit: replay must not call entry AI again.
    replay = _ingest(tmp_path, item)
    assert replay.candidate_job_ids == ()
    replay_ai = FakeAi()
    replay_report = await EntryDecider(
        tmp_path, ai=replay_ai, bot_username="tawg_bot", now=NOW
    ).decide(replay.candidate_job_ids)
    assert replay_report.ai_calls == 0
    assert replay_ai.calls == []

    commit_intake_state(tmp_path, receipts=replay.pending_receipts)
    committed = json.loads(receipt_path.read_text())
    assert committed["update_ids"] == [1]


@pytest.mark.asyncio
async def test_crash_before_entry_still_returns_candidate_on_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(update_id=2, message_id=11, text="ordinary group message")

    first = _ingest(tmp_path, item)
    assert first.candidate_job_ids == ("reply:tg:tawg:11",)
    # Simulate a crash between persist() and entry: no receipt, no entry.
    replay = _ingest(tmp_path, item)
    assert replay.candidate_job_ids == ("reply:tg:tawg:11",)

    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(replay.candidate_job_ids)
    assert report.ai_calls == 1
    commit_intake_state(tmp_path, receipts=replay.pending_receipts)


@pytest.mark.asyncio
async def test_every_new_message_gets_one_entry_call_without_batch_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    items = tuple(
        envelope(update_id=100 + index, message_id=200 + index, text=f"message {index}")
        for index in range(25)
    )
    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=items,
        now=NOW,
        telegram_chat_id=-100424242,
        defer_receipts=True,
    )
    assert len(result.candidate_job_ids) == 25

    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.candidates == 25
    assert report.ai_calls == 25
    assert len(ai.calls) == 25
    commit_intake_state(tmp_path, receipts=result.pending_receipts)


@pytest.mark.asyncio
async def test_explicit_failure_falls_back_without_a_second_entry_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(
        update_id=3,
        message_id=12,
        text="@tawg_bot please help me",
        triggers_reply=True,
        entities=(
            TelegramWebhookEntity(
                entity_type="mention", offset=0, length=9, value="@tawg_bot"
            ),
        ),
    )
    result = _ingest(tmp_path, item)
    ai = FakeAi(error=ClaudeCliError("model unavailable", kind="model"))

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 0
    assert len(ai.calls) == 1
    job = _jobs(tmp_path)["reply:tg:tawg:12"]
    assert job.classified_route is not None
    assert job.safe_error_code == "entry_model_failed"

    service = BotReplyService(
        tmp_path,
        ai=ai,
        bot_username="tawg_bot",
        entry_candidates_enabled=True,
    )
    prepared = await service.prepare(job.job_id, now=NOW)
    assert prepared is not None
    assert prepared.refusal is True
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_normal_failure_is_ignored_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(update_id=4, message_id=13, text="ordinary chatter")
    result = _ingest(tmp_path, item)
    ai = FakeAi(error=ClaudeCliError("model unavailable", kind="model"))

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 0
    job = _jobs(tmp_path)["reply:tg:tawg:13"]
    assert job.status is JobStatus.IGNORED
    assert job.classified_route is not None
    assert job.safe_error_code == "entry_model_failed"

    # A later empty maintenance pass has no candidate IDs and therefore no AI call.
    replay = _ingest(tmp_path, item)
    assert replay.candidate_job_ids == ()
    assert (await EntryDecider(
        tmp_path, ai=FakeAi(), bot_username="tawg_bot", now=NOW
    ).decide(replay.candidate_job_ids)).ai_calls == 0


@pytest.mark.asyncio
async def test_welcome_and_introduction_share_one_entry_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    joined = envelope(
        update_id=5,
        message_id=80,
        text="Happy to be here",
        public_username="LoganVerdict",
        display_name="Logan",
        message_thread_id=77,
    )
    welcome = envelope(
        update_id=6,
        message_id=81,
        text="Welcome, Logan 👋",
        reply_to_message_id=80,
        message_thread_id=77,
    )
    result = ingest_envelopes(
        root=tmp_path,
        group_slug="tawg",
        bot_username="tawg_bot",
        envelopes=(joined, welcome),
        now=NOW,
        telegram_chat_id=-100424242,
        defer_receipts=True,
    )
    assert set(result.candidate_job_ids) == {
        "reply:tg:tawg:80",
        "member-welcome:logan-verdict",
        "member-introduction:logan-verdict",
    }

    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 2
    assert len(ai.calls) == 2
    jobs = _jobs(tmp_path)
    assert jobs["member-welcome:logan-verdict"].routed_at is not None
    assert jobs["member-introduction:logan-verdict"].routed_at is not None
    assert jobs["member-welcome:logan-verdict"].status is JobStatus.PENDING
    assert jobs["member-introduction:logan-verdict"].status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_privacy_rejection_never_calls_entry_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    message_dir = tmp_path / "data/telegram/2026/08"
    message_dir.mkdir(parents=True)
    record = SourceRecord.from_text(
        record_id="tg:tawg:99",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:99",
        author_person_id="alice",
        author_source_handle="@alice_tawg",
        created_at=NOW,
        updated_at=NOW,
        text_original="-----BEGIN PRIVATE KEY-----\nsecret",
        ingested_at=NOW,
        source_payload={"message_kind": "group_message"},
    )
    (message_dir / "messages.jsonl").write_text(
        record.model_dump_json() + "\n", encoding="utf-8"
    )
    job = PendingBotJob(
        job_id="reply:tg:tawg:99",
        trigger_record_id="tg:tawg:99",
        reply_to_message_id=99,
        trigger_kind=TriggerKind.MESSAGE_CANDIDATE,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.persistence_payload()]) + "\n", encoding="utf-8"
    )

    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide([job.job_id])
    assert report.ai_calls == 0
    assert ai.calls == []
    updated = _jobs(tmp_path)[job.job_id]
    assert updated.safe_error_code == "privacy_rejected"
    assert updated.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_legacy_migration_routes_once_without_changing_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    message_dir = tmp_path / "data/telegram/2026/08"
    message_dir.mkdir(parents=True)
    record = SourceRecord.from_text(
        record_id="tg:tawg:101",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:101",
        author_person_id="alice",
        author_source_handle="@alice_tawg",
        created_at=NOW,
        updated_at=NOW,
        text_original="please answer this",
        ingested_at=NOW,
        source_payload={"message_kind": "group_message"},
    )
    (message_dir / "messages.jsonl").write_text(
        record.model_dump_json() + "\n", encoding="utf-8"
    )
    job = PendingBotJob(
        job_id="reply:tg:tawg:101",
        trigger_record_id="tg:tawg:101",
        reply_to_message_id=101,
        trigger_kind=TriggerKind.MENTION,
        status=JobStatus.PENDING,
        attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps([job.persistence_payload()]) + "\n", encoding="utf-8"
    )

    ai = FakeAi()
    first = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).migrate_legacy()
    assert first.ai_calls == 1
    assert len(ai.calls) == 1
    migrated = _jobs(tmp_path)[job.job_id]
    assert migrated.attempts == 3
    assert migrated.routed_at is not None

    second = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).migrate_legacy()
    assert second.ai_calls == 0
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_entry_publish_merges_latest_state_and_preserves_unrelated_jobs(
    tmp_path: Path,
) -> None:
    copy_static_runtime_tree(ROOT, tmp_path)
    updated = PendingBotJob(
        job_id="reply:tg:tawg:201",
        trigger_record_id="tg:tawg:201",
        reply_to_message_id=201,
        trigger_kind=TriggerKind.MESSAGE_CANDIDATE,
        created_at=NOW,
        updated_at=NOW,
    )
    unrelated = PendingBotJob(
        job_id="reply:tg:tawg:202",
        trigger_record_id="tg:tawg:202",
        reply_to_message_id=202,
        trigger_kind=TriggerKind.MENTION,
        status=JobStatus.READY,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps(
            [updated.persistence_payload(), unrelated.persistence_payload()]
        )
        + "\n",
        encoding="utf-8",
    )

    decider = EntryDecider(
        tmp_path, ai=FakeAi(), bot_username="tawg_bot", now=NOW
    )
    # Simulate a concurrent writer adding `unrelated` after the decision loaded jobs.
    decider._publish(
        {
            updated.job_id: updated.model_copy(
                update={
                    "classified_route": BotRoute.IGNORE,
                    "router_context_scope": RouteContextScope.CONVERSATION,
                    "router_context_sha256": "a" * 64,
                    "router_version": ENTRY_ROUTER_VERSION,
                    "routed_at": NOW,
                    "status": JobStatus.IGNORED,
                }
            )
        }
    )

    jobs = _jobs(tmp_path)
    assert set(jobs) == {updated.job_id, unrelated.job_id}
    assert jobs[unrelated.job_id] == unrelated
    assert jobs[updated.job_id].classified_route is BotRoute.IGNORE


@pytest.mark.asyncio
async def test_entry_mode_pure_greeting_ignore_does_not_force_a_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(update_id=7, message_id=14, text="good morning")
    result = _ingest(tmp_path, item)
    ai = FakeAi(route="ignore")

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 1
    job = _jobs(tmp_path)["reply:tg:tawg:14"]
    assert job.trigger_kind is TriggerKind.GREETING_CANDIDATE
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_entry_mode_pure_greeting_failure_does_not_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(update_id=8, message_id=15, text="hello")
    result = _ingest(tmp_path, item)
    ai = FakeAi(error=ClaudeCliError("model unavailable", kind="model"))

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 0
    assert len(ai.calls) == 1
    job = _jobs(tmp_path)["reply:tg:tawg:15"]
    assert job.trigger_kind is TriggerKind.GREETING_CANDIDATE
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED
    assert job.safe_error_code == "entry_model_failed"


@pytest.mark.asyncio
async def test_ordinary_chatter_without_needs_reply_or_bot_address_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=20, message_id=200, text="lunch was good today"),
    )
    ai = FakeAi(route="coordination", decision="reply", addressed_to_bot=False)

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)

    job = _jobs(tmp_path)["reply:tg:tawg:200"]
    assert report.ai_calls == 1
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_ordinary_explicit_ignore_decision_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=21, message_id=201, text="just chatting"),
    )
    ai = FakeAi(route="ignore", decision="ignore")

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:201"]
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_ordinary_needs_reply_category_replies_without_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=22, message_id=202, text="what is ERC-8183?"),
    )
    ai = FakeAi(route="knowledge_question", decision="reply", addressed_to_bot=False)

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:202"]
    assert job.classified_route is BotRoute.KNOWLEDGE_QUESTION
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_no_at_direct_to_bot_structured_judgment_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=23, message_id=203, text="hey trusty, can you help?"),
    )
    ai = FakeAi(route="coordination", decision="reply", addressed_to_bot=True)

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:203"]
    assert job.classified_route is BotRoute.COORDINATION
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_addressed_flag_cannot_authorize_privileged_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=24, message_id=204, text="hey bot, record this"),
    )
    ai = FakeAi(
        route="knowledge_correction",
        decision="reply",
        addressed_to_bot=True,
    )

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:204"]
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_explicit_mention_still_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(
            update_id=25,
            message_id=205,
            text="@tawg_bot good morning",
            triggers_reply=True,
            entities=(
                TelegramWebhookEntity(
                    entity_type="mention", offset=0, length=9, value="@tawg_bot"
                ),
            ),
        ),
    )
    ai = FakeAi(route="coordination", decision="reply", addressed_to_bot=True)

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:205"]
    assert job.trigger_kind is TriggerKind.MENTION
    assert job.classified_route is BotRoute.COORDINATION
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_reply_to_bot_still_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    (tmp_path / "data/state/delivery-state.json").write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": "reply:tg:tawg:899",
                    "job_id": "reply:tg:tawg:899",
                    "destination": "tawg",
                    "status": "delivered",
                    "content_sha256": "a" * 64,
                    "message_count": 1,
                    "delivery_format": "rich_markdown_v1",
                    "reply_to_message_id": 899,
                    "message_thread_id": 16,
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
    result = _ingest(
        tmp_path,
        envelope(
            update_id=28,
            message_id=208,
            text="here is the follow-up you asked for",
            reply_to_message_id=900,
        ),
    )
    ai = FakeAi(route="coordination", decision="reply", addressed_to_bot=True)

    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )

    job = _jobs(tmp_path)["reply:tg:tawg:208"]
    assert job.trigger_kind is TriggerKind.REPLY_TO_BOT
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_end_to_end_addressed_ordinary_message_reaches_prepared_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=26, message_id=206, text="hey trusty, can you help?"),
    )
    ai = ReplyFakeAi(route="coordination", decision="reply", addressed_to_bot=True)
    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )
    service = BotReplyService(
        tmp_path,
        ai=ai,
        bot_username="tawg_bot",
        entry_candidates_enabled=True,
    )

    prepared = await service.prepare("reply:tg:tawg:206", now=NOW)

    assert prepared is not None
    assert prepared.reply_text == "Hello!"
    assert any(call["job_type"] == "reply" for call in ai.calls)


@pytest.mark.asyncio
async def test_end_to_end_ordinary_chatter_never_reaches_reply_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    result = _ingest(
        tmp_path,
        envelope(update_id=27, message_id=207, text="lunch was good today"),
    )
    ai = ReplyFakeAi(route="coordination", decision="reply", addressed_to_bot=False)
    await EntryDecider(tmp_path, ai=ai, bot_username="tawg_bot", now=NOW).decide(
        result.candidate_job_ids
    )
    service = BotReplyService(
        tmp_path,
        ai=ai,
        bot_username="tawg_bot",
        entry_candidates_enabled=True,
    )

    prepared = await service.prepare("reply:tg:tawg:207", now=NOW)

    assert prepared is None
    assert not any(call["job_type"] == "reply" for call in ai.calls)


@pytest.mark.asyncio
async def test_greeting_text_cannot_bypass_ordinary_message_route_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    item = envelope(
        update_id=9,
        message_id=16,
        text="hello please record this in the knowledge base",
    )
    result = _ingest(tmp_path, item)
    ai = FakeAi(route="knowledge_correction")

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(result.candidate_job_ids)
    assert report.ai_calls == 1
    job = _jobs(tmp_path)["reply:tg:tawg:16"]
    assert job.trigger_kind is TriggerKind.GREETING_CANDIDATE
    assert job.classified_route is BotRoute.IGNORE
    assert job.status is JobStatus.IGNORED


@pytest.mark.asyncio
async def test_normal_candidate_ignore_does_not_ignore_welcome_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    message_dir = tmp_path / "data/telegram/2026/08"
    message_dir.mkdir(parents=True)
    record = SourceRecord.from_text(
        record_id="tg:tawg:301",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:301",
        author_person_id="alice",
        author_source_handle="@alice_tawg",
        created_at=NOW,
        updated_at=NOW,
        text_original="hello everyone",
        ingested_at=NOW,
        source_payload={"message_kind": "group_message"},
    )
    (message_dir / "messages.jsonl").write_text(
        record.model_dump_json() + "\n", encoding="utf-8"
    )
    normal = PendingBotJob(
        job_id="reply:tg:tawg:301",
        trigger_record_id="tg:tawg:301",
        reply_to_message_id=301,
        trigger_kind=TriggerKind.MESSAGE_CANDIDATE,
        created_at=NOW,
        updated_at=NOW,
    )
    welcome = PendingBotJob(
        job_id="member-welcome:logan",
        trigger_record_id="tg:tawg:301",
        reply_to_message_id=None,
        trigger_kind=TriggerKind.MEMBER_WELCOME,
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:301",
        created_at=NOW,
        updated_at=NOW,
    )
    introduction = PendingBotJob(
        job_id="member-introduction:logan",
        trigger_record_id="tg:tawg:301",
        reply_to_message_id=None,
        trigger_kind=TriggerKind.MEMBER_INTRODUCTION,
        welcome_target_person_id="logan",
        welcome_target_record_id="tg:tawg:301",
        prerequisite_job_id=welcome.job_id,
        created_at=NOW,
        updated_at=NOW,
    )
    (tmp_path / "data/state/pending-bot-jobs.json").write_text(
        json.dumps(
            [
                normal.persistence_payload(),
                welcome.persistence_payload(),
                introduction.persistence_payload(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ai = FakeAi(route="ignore")

    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide([normal.job_id, welcome.job_id, introduction.job_id])
    assert report.ai_calls == 1
    jobs = _jobs(tmp_path)
    assert jobs[normal.job_id].status is JobStatus.IGNORED
    assert jobs[welcome.job_id].status is JobStatus.PENDING
    assert jobs[introduction.job_id].status is JobStatus.PENDING
    assert jobs[welcome.job_id].routed_at is not None
    assert jobs[introduction.job_id].routed_at is not None


class FakePollingApi:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates

    async def get_updates_page(self, offset: int, limit: int = 100) -> list[dict[str, Any]]:
        del offset, limit
        return self.updates


class AckingPollingApi:
    """Models Telegram offset confirmation: the next request confirms lower ids."""

    def __init__(
        self,
        updates: list[dict[str, Any]],
        *,
        fail_on_offset: int | None = None,
    ) -> None:
        self.updates = list(updates)
        self.fail_on_offset = fail_on_offset
        self.calls: list[int] = []
        self.confirmed_below = 0

    async def get_updates_page(
        self, offset: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.calls.append(offset)
        self.confirmed_below = max(self.confirmed_below, offset)
        # Telegram permanently confirms updates below the offset of this request.
        self.updates = [
            update for update in self.updates if update["update_id"] >= offset
        ]
        if offset == self.fail_on_offset:
            raise RuntimeError("crash after Telegram confirmed the previous page")
        return self.updates[:limit]


def _polling_update(update_id: int, message_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(NOW.timestamp()),
            "chat": {"id": -100424242, "type": "supergroup", "title": "TAWG"},
            "from": {
                "id": 1042,
                "is_bot": False,
                "first_name": "Member",
                "username": "member_42",
            },
            "text": f"ordinary polling message {update_id}",
            "entities": [],
        },
    }


@pytest.mark.asyncio
async def test_polling_one_page_is_checkpointed_before_telegram_confirms_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    updates = [
        _polling_update(update_id, 1000 + update_id)
        for update_id in range(1, 121)
    ]
    api = AckingPollingApi(updates, fail_on_offset=101)
    intake = TelegramIntake(
        root=tmp_path,
        api=api,
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_bot",
    )

    first = await intake.collect(NOW)

    # Only page one was requested. A second getUpdates with offset 101 would
    # permanently confirm ids 1..100 before they were durably checkpointed.
    assert api.calls == [0]
    assert first.next_offset == 101
    assert len(first.candidate_job_ids) == 100

    # Crash before the cursor checkpoint: the same offset must replay page one,
    # not page two, because Telegram has not seen a greater offset yet.
    replay = await intake.collect(NOW)
    assert api.calls == [0, 0]
    assert set(replay.candidate_job_ids) == set(first.candidate_job_ids)
    assert api.confirmed_below == 0

    # Durable checkpoint happens after entry decisions; only then may the next
    # request confirm page one and read page two. Disable the injected crash so the
    # now-safe page-two request can complete.
    commit_intake_state(tmp_path, cursors=replay.pending_cursors)
    api.fail_on_offset = None
    third = await intake.collect(NOW)
    assert api.calls == [0, 0, 101]
    assert api.confirmed_below == 101
    assert len(third.candidate_job_ids) == 20
    assert third.next_offset == 121


@pytest.mark.asyncio
async def test_polling_crash_before_entry_still_returns_candidate_on_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    copy_static_runtime_tree(ROOT, tmp_path)
    update = {
        "update_id": 555,
        "message": {
            "message_id": 42,
            "date": int(NOW.timestamp()),
            "chat": {"id": -100424242, "type": "supergroup", "title": "TAWG"},
            "from": {
                "id": 1042,
                "is_bot": False,
                "first_name": "Member",
                "username": "member_42",
            },
            "text": "ordinary polling message",
            "entities": [],
        },
    }
    intake = TelegramIntake(
        root=tmp_path,
        api=FakePollingApi([update]),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_bot",
    )
    first = await intake.collect(NOW)
    assert first.candidate_job_ids == ("reply:tg:tawg:42",)
    cursor_path = tmp_path / "data/state/source-cursors.json"
    assert json.loads(cursor_path.read_text())["telegram_offset"] == 0

    # Crash before entry: the cursor is still old, so the same update is replayed
    # and the un-entered job must still be a candidate.
    replay = await intake.collect(NOW)
    assert replay.candidate_job_ids == ("reply:tg:tawg:42",)
    ai = FakeAi()
    report = await EntryDecider(
        tmp_path, ai=ai, bot_username="tawg_bot", now=NOW
    ).decide(replay.candidate_job_ids)
    assert report.ai_calls == 1
    commit_intake_state(tmp_path, cursors=replay.pending_cursors)
    assert json.loads(cursor_path.read_text())["telegram_offset"] == 556

    # After entry and cursor commit, the same update no longer produces a candidate.
    after = await intake.collect(NOW)
    assert after.candidate_job_ids == ()


def test_entry_candidates_flag_defaults_off_and_requires_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAWG_ENTRY_CANDIDATES_ENABLED", raising=False)
    assert entry_candidates_enabled() is False
    monkeypatch.setenv("TAWG_ENTRY_CANDIDATES_ENABLED", "true")
    assert entry_candidates_enabled() is True
