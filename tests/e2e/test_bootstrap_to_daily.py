from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.daily import DailyReadiness, DailyService, DailyWindow
from tawg_bot.daily_evidence import DailyEvidence
from tawg_bot.delivery import DeliveryService
from tawg_bot.github_source import GitHubSource
from tawg_bot.knowledge_jobs import KnowledgeRefreshJob
from tawg_bot.knowledge_refresh import KnowledgeRefresh
from tawg_bot.magicians_source import MagiciansSource, TopicSeed
from tawg_bot.models import SourceCursors, SourceRecord
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.query import SourceQuery
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.source_registry import SourceRegistry
from tawg_bot.storage import JsonlCollection
from tawg_bot.telegram_export import TelegramDesktopImporter
from tawg_bot.telegram_intake import TelegramIntake
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tests.integration.test_delivery_retries import Api as DeliveryApi
from tests.integration.test_github_sync import FakeGitHubClient
from tests.integration.test_live_knowledge_refresh import (
    JOB_KEY,
)
from tests.integration.test_live_knowledge_refresh import (
    _pack as live_pack,
)
from tests.integration.test_live_knowledge_refresh import (
    _result as live_knowledge_output,
)
from tests.integration.test_magicians_sync import TopicClient
from tests.integration.test_telegram_cursor import FakeTelegramApi, fixture_updates
from tests.support.runtime_repository import initialize_empty_runtime_state
from tests.unit.test_delivery import Checkpoint

ROOT = Path(__file__).parents[2]
RUN_AT = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)
WINDOW = DailyWindow.for_due_run(RUN_AT)


class FakeAi:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        context = json.loads(kwargs["context_pack"])
        assert context["context_schema"] == "tawg.context-pack.v1"
        assert context["source_content_is_untrusted"] is True
        expected_schema = {
            "knowledge": "tawg.knowledge-result.v2",
            "daily": "tawg.daily-result.v1",
        }[kwargs["job_type"]]
        assert context["output_schema"]["properties"]["schema_version"]["const"] == (
            expected_schema
        )
        if kwargs["job_type"] == "knowledge":
            assert context["evidence_pack"]["schema_version"] == "tawg.evidence-batch.v1"
        else:
            assert context["trigger"]["operation"] == "prepare_daily_catch_up"
        self.calls.append(kwargs)
        return deepcopy(self.outputs.pop(0))


class E2eLiveEvidence:
    async def build(self, query, *, now):
        assert now == RUN_AT
        return live_pack().model_copy(update={"query": query})


def scaffold(root: Path) -> None:
    for relative in ("config", "prompts", "bot-skill", "src/tawg_bot/schemas"):
        source = ROOT / relative
        target = root / relative
        if source.is_dir():
            shutil.copytree(source, target)
    meta = root / "knowledge/meta"
    meta.mkdir(parents=True)
    (root / "knowledge/index.md").write_text(
        page("TAWG Knowledge Index", "index", "Bootstrap pending.", []),
        encoding="utf-8",
    )
    (root / "knowledge/hot.md").write_text(
        page("TAWG Hot Context", "meta", "Bootstrap pending.", []),
        encoding="utf-8",
    )
    (meta / "aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n",
        encoding="utf-8",
    )
    (meta / "sources.yml").write_bytes((ROOT / "knowledge/meta/sources.yml").read_bytes())
    (meta / "source-ledger.json").write_text(
        '{"schema":"tawg.source-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    (meta / "claim-ledger.json").write_text(
        '{"schema":"tawg.claim-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    initialize_empty_runtime_state(root)


def page(title: str, page_type: str, body: str, citations: list[str]) -> str:
    source_ids = "\n".join(f"  - {source_id}" for source_id in citations)
    return (
        f"---\ntitle: {title}\ntype: {page_type}\ncreated: 2026-08-23\n"
        f"updated: 2026-08-24\nsource_ids:\n{source_ids}\n---\n\n"
        f"# {title}\n\n{body}\n"
    )


def knowledge_output(root: Path, operation_id: str) -> dict[str, Any]:
    records = SourceQuery(root).records()
    citation = next(record.record_id for record in records if "ERC-8004" in record.text_original)
    pages = {
        "knowledge/index.md": page(
            "TAWG Knowledge Index",
            "index",
            "See [[hot]] and [[ercs/erc-8004]].",
            [citation],
        ),
        "knowledge/hot.md": page(
            "TAWG Hot Context", "meta", "Current focus: [[ercs/erc-8004]].", [citation]
        ),
        "knowledge/ercs/erc-8004.md": page(
            "ERC-8004",
            "erc",
            "The group is clarifying verifiable validation behavior.",
            [citation],
        ),
    }
    source_ledger = {
        "schema": "tawg.source-ledger.v1",
        "entries": {
            record.record_id: {
                "authority": "community",
                "independence_key": record.source_type.value,
                "active": True,
                "fresh_until": "2030-01-01T00:00:00Z",
            }
            for record in records
        },
    }
    writes: list[dict[str, Any]] = []
    contents = {
        **pages,
        "knowledge/meta/source-ledger.json": json.dumps(source_ledger, sort_keys=True),
        "knowledge/meta/claim-ledger.json": json.dumps(
            {"schema": "tawg.claim-ledger.v1", "entries": {}}, sort_keys=True
        ),
    }
    for path, content in contents.items():
        target = root / path
        writes.append(
            {
                "path": path,
                "expected_sha256": (
                    hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
                ),
                "content": content,
                "citations": [citation] if path.endswith(".md") else [],
            }
        )
    summaries = {
        record.record_id: "The contributor organized new discussion points."
        for record in records
        if any(ord(character) > 127 for character in record.text_original)
    }
    return {
        "schema_version": "tawg.knowledge-result.v1",
        "transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": operation_id,
            "writes": writes,
        },
        "english_summaries": summaries,
    }


@pytest.mark.asyncio
async def test_bootstrap_backfill_delayed_refresh_and_daily_delivery(tmp_path: Path) -> None:
    scaffold(tmp_path)
    history_uow = RepositoryUnitOfWork(tmp_path, operation_id="acceptance-history")
    history = TelegramDesktopImporter.for_repository(tmp_path).import_file(
        ROOT / "tests/fixtures/telegram_export.json",
        group_slug="tawg",
        uow=history_uow,
    )
    history_uow.publish()
    assert history.imported == 5

    github = GitHubSource.for_repository(
        root=tmp_path,
        client=FakeGitHubClient(),
        now=lambda: datetime(2026, 8, 23, 0, 30, tzinfo=UTC),
    )
    repository = (await github.list_public_repositories())[0]
    github_batch = await github.sync_repository(repository, {})
    assert github_batch.successful
    assert github_batch.records

    magicians = MagiciansSource(
        client=TopicClient(),
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(tmp_path / "config/privacy.yml"),
        now=lambda: datetime(2026, 8, 23, 1, tzinfo=UTC),
    )
    magicians_batch = await magicians.sync_all(
        [TopicSeed(25098, "erc-8004-trustless-agents", "configured")],
        SourceCursors(),
    )
    assert magicians_batch.records
    assert not (tmp_path / "data/github").exists()
    assert not (tmp_path / "data/magicians").exists()

    intake = TelegramIntake(
        root=tmp_path,
        api=FakeTelegramApi(fixture_updates()),
        chat_id=-100424242,
        group_slug="tawg",
        bot_username="tawg_helper",
    )
    live = await intake.collect(datetime(2026, 8, 24, 1, tzinfo=UTC))
    assert live.next_offset == 107
    assert live.jobs_created == 2
    telegram_path = tmp_path / "data/telegram/2026/08/messages.jsonl"
    post_cutoff = SourceRecord.from_text(
        record_id="tg:tawg:post-cutoff",
        source_type="telegram_message",
        source_locator=("repo:data/telegram/2026/08/messages.jsonl#tg:tawg:post-cutoff"),
        author_person_id="alice",
        author_source_handle="alice",
        created_at=datetime(2026, 8, 23, 23, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 23, 23, 1, tzinfo=UTC),
        text_original="This belongs to the next Daily window.",
        ingested_at=RUN_AT,
    )
    telegram_path.write_bytes(
        JsonlCollection(telegram_path, SourceRecord).merged_bytes([post_cutoff])
    )

    operation_id = "acceptance-knowledge-refresh"
    refresh_job = KnowledgeRefreshJob(
        job_key=JOB_KEY,
        erc_number=8004,
        source_key="erc-8004-canonical",
        observed_version="fixture-v1",
        observed_sha256="0" * 64,
        created_at=RUN_AT,
        updated_at=RUN_AT,
    )
    (tmp_path / "data/state/pending-knowledge-refresh.json").write_text(
        json.dumps([refresh_job.model_dump(mode="json")]) + "\n", encoding="utf-8"
    )
    ai = FakeAi(
        [
            live_knowledge_output(tmp_path, operation_id),
            json.loads((ROOT / "tests/fixtures/ai/daily-active.json").read_text()),
        ]
    )
    registry = SourceRegistry.from_yaml(tmp_path / "knowledge/meta/sources.yml")
    refreshed = await KnowledgeRefresh(
        tmp_path,
        ai=ai,
        live_evidence=E2eLiveEvidence(),
        registry=registry,
    ).run(cutoff=RUN_AT, operation_id=operation_id)
    assert refreshed.processed_job_keys == (JOB_KEY,)
    assert {
        path.relative_to(tmp_path).as_posix() for path in (tmp_path / "knowledge").rglob("*.md")
    } == {
        "knowledge/index.md",
        "knowledge/hot.md",
        "knowledge/ercs/erc-8004.md",
    }

    records = SourceQuery(tmp_path).records()
    assert SourceQuery(tmp_path).search(topic="ERC-8004")
    assert SourceQuery(tmp_path).search(person_id="alice")
    assert SourceQuery(tmp_path).search(start=WINDOW.start, end=WINDOW.end)
    assert any(
        hit.path == "knowledge/ercs/erc-8004.md"
        for hit in VaultRetriever(tmp_path).query("ERC-8004 validation")
    )
    assert any(record.record_id == "tg:tawg:post-cutoff" for record in records)

    ready = DailyReadiness(
        telegram_synced_at=RUN_AT,
        live_evidence_collected_at=RUN_AT,
        knowledge_refreshed_at=RUN_AT,
    )
    telegram_evidence = tuple(
        DailyEvidence(
            evidence_id=record.record_id,
            source_kind="telegram",
            source_url=record.source_locator,
            created_at=record.created_at,
            updated_at=record.updated_at,
            author_person_id=record.author_person_id,
            text=record.text_original,
        )
        for record in records
        if WINDOW.contains(record.updated_at) and record.text_original.strip()
    )
    github_marker = github_batch.records[0].text_original
    magicians_marker = magicians_batch.records[-1].text_original
    live_external_evidence = tuple(
        DailyEvidence(
            evidence_id=record.record_id,
            source_kind=source_kind,
            source_url=record.source_locator,
            created_at=datetime(2026, 8, 23, 12 + index, tzinfo=UTC),
            updated_at=datetime(2026, 8, 23, 12 + index, tzinfo=UTC),
            author_person_id=record.author_person_id,
            text=record.text_original,
        )
        for index, (source_kind, record) in enumerate(
            (
                ("github", github_batch.records[0]),
                ("magicians", magicians_batch.records[-1]),
            )
        )
    )
    daily_evidence = (*telegram_evidence, *live_external_evidence)
    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=ready, evidence=daily_evidence
    )
    assert prepared is not None
    assert "post-cutoff" not in ai.calls[-1]["context_pack"]
    assert github_marker in ai.calls[-1]["context_pack"]
    assert magicians_marker in ai.calls[-1]["context_pack"]
    assert not (tmp_path / "data/github").exists()
    assert not (tmp_path / "data/magicians").exists()

    api = DeliveryApi()
    delivered = await DeliveryService(
        tmp_path, api=api, chat_id=-100424242, checkpoint=Checkpoint()
    ).deliver(
        job_id=prepared.window_id,
        text=prepared.telegram_text,
        reply_to_message_id=None,
        now=RUN_AT,
    )
    assert delivered.status.value == "delivered"
    assert api.calls
