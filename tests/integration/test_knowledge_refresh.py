from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tawg_bot.knowledge_refresh import KnowledgeRefresh, KnowledgeRefreshRejected
from tawg_bot.models import SourceCursors, SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection

PROJECT = Path(__file__).parents[2]
CUTOFF = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


class FakeAi:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.result)


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _record(
    record_id: str,
    source_type: SourceType,
    text: str,
    at: datetime,
    locator: str,
) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=source_type,
        source_locator=locator,
        author_person_id="alice",
        author_source_handle="alice",
        created_at=at,
        updated_at=at,
        text_original=text,
        ingested_at=at,
    )


def _seed(root: Path) -> tuple[str, ...]:
    (root / "config").mkdir()
    (root / "config/privacy.yml").write_bytes((PROJECT / "config/privacy.yml").read_bytes())
    for relative in (
        "src/tawg_bot/schemas/knowledge-result.v1.json",
        "prompts/knowledge-system.md",
        "bot-skill/SKILL.md",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    (root / "knowledge/meta").mkdir(parents=True)
    (root / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    (root / "knowledge/meta/source-ledger.json").write_text(
        '{"schema":"tawg.source-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    (root / "knowledge/meta/claim-ledger.json").write_text(
        '{"schema":"tawg.claim-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    seed_page = (
        "---\ntitle: Seed\ntype: meta\ncreated: 2026-08-23\nupdated: 2026-08-23\n"
        "---\n\n# Seed\n"
    )
    (root / "knowledge/index.md").write_text(seed_page, encoding="utf-8")
    (root / "knowledge/hot.md").write_text(seed_page, encoding="utf-8")
    cursor_path = root / "data/state/source-cursors.json"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text(
        SourceCursors().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    records = (
        _record(
            "tg:tawg:1",
            SourceType.TELEGRAM_MESSAGE,
            "我建议改进 ERC-8004 validation flow。",
            datetime(2026, 8, 22, 23, 10, tzinfo=UTC),
            "repo:data/telegram/2026/08/messages.jsonl#tg:tawg:1",
        ),
        _record(
            "gh:agent-ercs:commit:abc",
            SourceType.GITHUB_COMMIT,
            "Clarify ERC-8183 settlement invariants.",
            datetime(2026, 8, 23, 0, 20, tzinfo=UTC),
            "https://github.com/trustless-ai/agent-ercs/commit/abc",
        ),
        _record(
            "magicians:25098:post:3",
            SourceType.MAGICIANS_POST,
            "ERC-8004 should retain verifiable on-chain reads.",
            datetime(2026, 8, 23, 0, 40, tzinfo=UTC),
            "https://ethereum-magicians.org/t/25098/3",
        ),
    )
    locations = (
        "data/telegram/2026/08/messages.jsonl",
        "data/github/agent-ercs/2026/08/records.jsonl",
        "data/magicians/2026/08/posts.jsonl",
    )
    for relative, record in zip(locations, records, strict=True):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JsonlCollection(path, SourceRecord).merged_bytes([record]))
    return tuple(record.record_id for record in records)


def _page(title: str, page_type: str, body: str, source_ids: tuple[str, ...]) -> str:
    sources = "\n".join(f"  - {source_id}" for source_id in source_ids)
    return (
        f"---\ntitle: {title}\ntype: {page_type}\ncreated: 2026-08-23\n"
        f"updated: 2026-08-23\nsource_ids:\n{sources}\n---\n\n# {title}\n\n{body}\n"
    )


def _valid_result(root: Path, operation_id: str, source_ids: tuple[str, ...]) -> dict[str, Any]:
    pages = {
        "knowledge/index.md": _page(
            "TAWG Knowledge Index",
            "index",
            "See [[hot]], [[people/alice]], [[ercs/erc-8004]], [[topics/validation]], "
            "[[repos/agent-ercs]], and [[timeline/2026-08]].",
            source_ids,
        ),
        "knowledge/hot.md": _page(
            "TAWG Hot Context", "meta", "Current focus: [[ercs/erc-8004]].", source_ids
        ),
        "knowledge/people/alice.md": _page(
            "Alice", "person", "Alice proposed a validation improvement.", source_ids
        ),
        "knowledge/ercs/erc-8004.md": _page(
            "ERC-8004", "erc", "Current work retains verifiable on-chain reads.", source_ids
        ),
        "knowledge/topics/validation.md": _page(
            "Validation", "topic", "Validation work spans ERC-8004 and ERC-8183.", source_ids
        ),
        "knowledge/repos/agent-ercs.md": _page(
            "agent-ercs",
            "repository",
            "The repository clarified settlement invariants.",
            source_ids,
        ),
        "knowledge/timeline/2026-08.md": _page(
            "August 2026",
            "timeline",
            "The working group advanced validation semantics.",
            source_ids,
        ),
    }
    source_entries = {
        source_id: {
            "authority": "community" if source_id.startswith("tg:") else "primary",
            "independence_key": source_id.split(":", 1)[0],
            "active": True,
            "fresh_until": "2030-01-01T00:00:00Z",
        }
        for source_id in source_ids
    }
    ledgers = {
        "knowledge/meta/source-ledger.json": json.dumps(
            {"schema": "tawg.source-ledger.v1", "entries": source_entries},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "knowledge/meta/claim-ledger.json": json.dumps(
            {
                "schema": "tawg.claim-ledger.v1",
                "entries": {
                    "erc-8004-current": {
                        "state": "accepted",
                        "risk": "ordinary",
                        "source_ids": [source_ids[2]],
                        "assessed_at": "2026-08-23T01:00:00Z",
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    }
    writes = [
        {
            "path": path,
            "expected_sha256": _hash(root / path),
            "content": content,
            "citations": list(source_ids) if path.endswith(".md") else [],
        }
        for path, content in {**pages, **ledgers}.items()
    ]
    return {
        "schema_version": "tawg.knowledge-result.v1",
        "transaction": {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": operation_id,
            "writes": writes,
        },
        "english_summaries": {
            "tg:tawg:1": "Alice suggested improving the ERC-8004 validation flow."
        },
    }


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for base in (root / "knowledge", root / "data/state")
        for path in base.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_refresh_publishes_one_atomic_vault_update_and_replay_is_noop(
    tmp_path: Path,
) -> None:
    source_ids = _seed(tmp_path)
    operation_id = "knowledge-refresh-20260823t010000z"
    source_before = {
        path: (tmp_path / path).read_bytes()
        for path in (
            "data/telegram/2026/08/messages.jsonl",
            "data/github/agent-ercs/2026/08/records.jsonl",
            "data/magicians/2026/08/posts.jsonl",
        )
    }
    ai = FakeAi(_valid_result(tmp_path, operation_id, source_ids))
    service = KnowledgeRefresh(tmp_path, ai=ai)

    first = await service.run(cutoff=CUTOFF, operation_id=operation_id)
    second = await service.run(cutoff=CUTOFF, operation_id="knowledge-refresh-replay")

    assert first.processed_record_ids == source_ids
    assert first.changed_paths
    assert second.processed_record_ids == ()
    assert len(ai.calls) == 1
    assert "我建议改进" in ai.calls[0]["context_pack"]
    assert '"source_content_is_untrusted":true' in ai.calls[0]["context_pack"]
    assert (tmp_path / ".vault-meta/bm25.json").is_file()
    assert SourceCursors.model_validate_json(
        (tmp_path / "data/state/source-cursors.json").read_text()
    ).knowledge_record_id == source_ids[-1]
    for path, payload in source_before.items():
        assert (tmp_path / path).read_bytes() == payload


@pytest.mark.asyncio
async def test_refresh_rejects_missing_english_summary_without_writes(tmp_path: Path) -> None:
    source_ids = _seed(tmp_path)
    result = _valid_result(tmp_path, "knowledge-refresh", source_ids)
    result["english_summaries"] = {}
    before = _snapshot(tmp_path)

    with pytest.raises(KnowledgeRefreshRejected, match="English summary"):
        await KnowledgeRefresh(tmp_path, ai=FakeAi(result)).run(
            cutoff=CUTOFF, operation_id="knowledge-refresh"
        )

    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack",
    [
        "prompt_injection_path",
        "fabricated_source",
        "unsupported_claim",
        "oversized_output",
        "privacy",
    ],
)
async def test_adversarial_model_output_cannot_change_vault_or_cursor(
    tmp_path: Path, attack: str
) -> None:
    source_ids = _seed(tmp_path)
    result = _valid_result(tmp_path, "knowledge-refresh", source_ids)
    if attack == "prompt_injection_path":
        result["transaction"]["writes"][0]["path"] = ".github/workflows/pwn.yml"
    elif attack == "fabricated_source":
        ledger = next(
            write
            for write in result["transaction"]["writes"]
            if write["path"] == "knowledge/meta/source-ledger.json"
        )
        raw = json.loads(ledger["content"])
        raw["entries"]["made-up:source"] = raw["entries"][source_ids[0]]
        ledger["content"] = json.dumps(raw)
    elif attack == "unsupported_claim":
        ledger = next(
            write
            for write in result["transaction"]["writes"]
            if write["path"] == "knowledge/meta/claim-ledger.json"
        )
        raw = json.loads(ledger["content"])
        raw["entries"]["erc-8004-current"]["source_ids"] = []
        ledger["content"] = json.dumps(raw)
    elif attack == "oversized_output":
        result["transaction"]["writes"][0]["content"] += "x" * 600_000
        result["transaction"]["writes"][1]["content"] += "x" * 600_000
    else:
        result["transaction"]["writes"][0]["content"] += "\nprivate@example.com\n"
    before = _snapshot(tmp_path)

    with pytest.raises(KnowledgeRefreshRejected):
        await KnowledgeRefresh(tmp_path, ai=FakeAi(result)).run(
            cutoff=CUTOFF, operation_id="knowledge-refresh"
        )

    assert _snapshot(tmp_path) == before
