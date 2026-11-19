from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tawg_bot.bot_router import BotReplyService
from tawg_bot.models import DeliveryAttempt

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def _attempt(
    delivery_id: str,
    *,
    telegram_message_ids: list[int],
    updated_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "delivery_id": delivery_id,
        "job_id": delivery_id,
        "status": "delivered",
        "content_sha256": "a" * 64,
        "message_count": 1,
        "telegram_message_ids": telegram_message_ids,
        "prepared_at": NOW.isoformat(),
        "updated_at": updated_at.isoformat(),
        "sent_at": updated_at.isoformat(),
    }


def _write_state(root: Path, name: str, records: list[dict[str, Any]]) -> None:
    path = root / "data/state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records) + "\n", encoding="utf-8")


def _load(root: Path) -> tuple[DeliveryAttempt, ...]:
    service = object.__new__(BotReplyService)
    service.root = root.resolve()
    return service._load_delivery_attempts()


def test_mirrored_delivery_state_files_are_deduplicated_by_delivery_id(
    tmp_path: Path,
) -> None:
    _write_state(
        tmp_path,
        "delivery-state.8750877254.json",
        [
            _attempt("github-announcement:1", telegram_message_ids=[8001]),
            _attempt("mirror-only", telegram_message_ids=[8002]),
        ],
    )
    _write_state(
        tmp_path,
        "delivery-state.json",
        [
            _attempt("github-announcement:1", telegram_message_ids=[9001]),
            _attempt("main-only", telegram_message_ids=[9002]),
        ],
    )

    attempts = _load(tmp_path)

    by_id = {attempt.delivery_id: attempt for attempt in attempts}
    assert len(attempts) == 3
    assert set(by_id) == {"github-announcement:1", "mirror-only", "main-only"}
    # Sorted paths read the mirror first and delivery-state.json last, so the main
    # file's record wins for a repeated delivery id.
    assert by_id["github-announcement:1"].telegram_message_ids == [9001]


def test_identical_duplicate_delivery_records_collapse_to_one(tmp_path: Path) -> None:
    record = _attempt("github-announcement:42", telegram_message_ids=[7])
    _write_state(tmp_path, "delivery-state.8750877254.json", [record])
    _write_state(tmp_path, "delivery-state.json", [record])

    attempts = _load(tmp_path)

    assert attempts == (DeliveryAttempt.model_validate(record),)
