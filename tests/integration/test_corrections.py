from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from tawg_bot.bot_router import BotReplyService
from tawg_bot.models import SourceRecord
from tawg_bot.storage import JsonlCollection
from tests.integration.test_bot_replies import NOW, FakeAi, seed


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.asyncio
async def test_supported_correction_replaces_current_fact_without_touching_sources(
    tmp_path: Path,
) -> None:
    job = seed(tmp_path, "@bot correction: validation is opt-in, not mandatory")
    page_path = tmp_path / "knowledge/ercs/erc-8004.md"
    page_path.parent.mkdir(parents=True)
    current = (
        "---\ntitle: ERC-8004\ntype: erc\ncreated: 2026-08-23\nupdated: 2026-08-23\n"
        "source_ids:\n  - tg:tawg:10\n---\n\n# ERC-8004\n\nValidation is mandatory.\n"
    )
    corrected = current.replace("mandatory", "opt-in")
    page_path.write_text(current, encoding="utf-8")
    source_path = tmp_path / "data/telegram/2026/08/messages.jsonl"
    source_before = source_path.read_bytes()
    result = {
        "schema_version": "tawg.reply-result.v1",
        "reply_text": "Thanks—the cited discussion supports updating the current page.",
        "language": "en",
        "english_recap": None,
        "citations": ["tg:tawg:10"],
        "correction_transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": job.job_id,
            "writes": [
                {
                    "path": "knowledge/ercs/erc-8004.md",
                    "expected_sha256": _sha(current),
                    "content": corrected,
                    "citations": ["tg:tawg:10"],
                }
            ],
        },
        "refusal": False,
    }

    prepared = await BotReplyService(
        tmp_path, ai=FakeAi(result), bot_username="bot"
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert "supports updating" in prepared.reply_text
    assert page_path.read_text() == corrected
    assert source_path.read_bytes() == source_before
    assert not list((tmp_path / "knowledge").rglob("*correction*.md"))
    assert JsonlCollection(source_path, SourceRecord).decode(source_path.read_bytes())


@pytest.mark.asyncio
async def test_ambiguous_correction_asks_for_evidence_without_writing(tmp_path: Path) -> None:
    job = seed(tmp_path, "@bot correction: the page is wrong")
    result = {
        "schema_version": "tawg.reply-result.v1",
        "reply_text": "Could you share the source and the exact sentence that should change?",
        "language": "en",
        "english_recap": None,
        "citations": [],
        "correction_transaction": None,
        "refusal": False,
    }
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (tmp_path / "knowledge").rglob("*")
        if path.is_file()
    }

    await BotReplyService(tmp_path, ai=FakeAi(result), bot_username="bot").prepare(
        job.job_id, now=NOW + timedelta(minutes=2)
    )

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (tmp_path / "knowledge").rglob("*")
        if path.is_file()
    }
    assert after == before
