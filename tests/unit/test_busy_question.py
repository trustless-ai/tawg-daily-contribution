from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.busy_question import (
    AskQuestionResult,
    BusyQuestionConfig,
    BusyQuestionService,
    BusyQuestionState,
)
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _record(record_id: str, at: datetime) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/2026/09/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=at,
        updated_at=at,
        text_original="hello",
        ingested_at=at,
    )


def seed_messages(root: Path, timestamps: list[datetime]) -> None:
    path = root / "data/telegram/2026/09/messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [_record(f"tg:tawg:{i}", ts) for i, ts in enumerate(timestamps)]
    path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes(records))


def config(*, threshold: int = 14, cooldown: int = 7200) -> BusyQuestionConfig:
    return BusyQuestionConfig(
        window_seconds=7200,
        threshold=threshold,
        cooldown_seconds=cooldown,
        target="@Tmerlini_bot",
    )


def test_config_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAWG_BUSY_QUESTION_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("TAWG_BUSY_QUESTION_THRESHOLD", "20")
    monkeypatch.setenv("TAWG_BUSY_QUESTION_COOLDOWN_SECONDS", "5400")
    monkeypatch.setenv("TAWG_BUSY_QUESTION_TARGET", "@some_bot")
    parsed = BusyQuestionConfig.from_env()
    assert parsed.window_seconds == 3600
    assert parsed.threshold == 20
    assert parsed.cooldown_seconds == 5400
    assert parsed.target == "@some_bot"


def test_count_recent_messages_only_counts_window(tmp_path: Path) -> None:
    seed_messages(
        tmp_path,
        [
            NOW - timedelta(hours=3),
            NOW - timedelta(minutes=90),
            NOW - timedelta(minutes=30),
            NOW,
        ],
    )
    service = BusyQuestionService(tmp_path, config=config())
    assert service.count_recent_messages(NOW) == 3  # excludes the 3h-old message


def test_decide_asks_when_threshold_and_cooldown_ok(tmp_path: Path) -> None:
    seed_messages(tmp_path, [NOW - timedelta(minutes=m) for m in (5, 10, 15, 20, 25)])
    service = BusyQuestionService(tmp_path, config=config(threshold=5))
    decision = service.decide(NOW)
    assert decision.should_ask is True
    assert decision.recent_count == 5


def test_decide_withholds_when_below_threshold(tmp_path: Path) -> None:
    seed_messages(tmp_path, [NOW - timedelta(minutes=5)])
    service = BusyQuestionService(tmp_path, config=config(threshold=5))
    assert service.decide(NOW).should_ask is False


def test_decide_withholds_during_cooldown(tmp_path: Path) -> None:
    seed_messages(tmp_path, [NOW - timedelta(minutes=m) for m in (5, 10, 15, 20, 25)])
    (tmp_path / "data/state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/state/busy-question-state.json").write_text(
        BusyQuestionState(last_triggered_at=NOW - timedelta(minutes=30)).model_dump_json(),
        encoding="utf-8",
    )
    service = BusyQuestionService(tmp_path, config=config(threshold=5, cooldown=7200))
    assert service.decide(NOW).should_ask is False


def test_decide_asks_after_cooldown_elapses(tmp_path: Path) -> None:
    seed_messages(tmp_path, [NOW - timedelta(minutes=m) for m in (5, 10, 15, 20, 25)])
    (tmp_path / "data/state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/state/busy-question-state.json").write_text(
        BusyQuestionState(last_triggered_at=NOW - timedelta(hours=3)).model_dump_json(),
        encoding="utf-8",
    )
    service = BusyQuestionService(tmp_path, config=config(threshold=5, cooldown=7200))
    assert service.decide(NOW).should_ask is True


def test_ask_question_result_parses() -> None:
    result = AskQuestionResult.model_validate(
        {
            "schema_version": "tawg.ask_question-result.v1",
            "question_text": "what did I miss?",
        }
    )
    assert result.question_text == "what did I miss?"


def test_stage_triggered_writes_state(tmp_path: Path) -> None:
    from tawg_bot.unit_of_work import RepositoryUnitOfWork

    service = BusyQuestionService(tmp_path, config=config())
    uow = RepositoryUnitOfWork(tmp_path, operation_id="busy-question:test")
    uow.register_external_evidence(())
    service.stage_triggered(uow, NOW)
    uow.publish()
    state = json.loads(
        (tmp_path / "data/state/busy-question-state.json").read_text(encoding="utf-8")
    )
    assert state["last_triggered_at"] == NOW.isoformat().replace("+00:00", "Z")
