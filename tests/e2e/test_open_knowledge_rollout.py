from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tawg_bot.bot_router import BotReplyService
from tawg_bot.scan_targets import ScanTargetStore
from tests.integration.test_bot_replies import NOW, FakeAi, seed
from tests.integration.test_erc_scan_registration import (
    MAGICIANS,
    FakeVerifier,
    _registration_result,
    _seed_registry,
)


@pytest.mark.asyncio
async def test_one_reply_atomically_records_knowledge_and_registers_complete_erc(
    tmp_path: Path,
) -> None:
    job = seed(tmp_path, f"@bot record ERC-8183 and scan {MAGICIANS}")
    _seed_registry(tmp_path)

    prepared = await BotReplyService(
        tmp_path,
        ai=FakeAi(
            _registration_result(
                job.job_id,
                job.trigger_record_id,
                proposal_pr_url=None,
            ),
            route="knowledge_correction",
        ),
        bot_username="bot",
        scan_target_verifier=FakeVerifier(),
    ).prepare(job.job_id, now=NOW + timedelta(minutes=2))

    assert prepared.reply_to_message_id == 12
    assert (tmp_path / "knowledge/topics/agentic-commerce.md").is_file()
    target = ScanTargetStore(tmp_path).load().ercs[0]
    assert target.erc_number == 8183
    assert target.registered_from_record_id == job.trigger_record_id
    job_state = json.loads(
        (tmp_path / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
    )[0]
    assert job_state["status"] == "ready"
    assert job_state["safe_error_code"] is None
