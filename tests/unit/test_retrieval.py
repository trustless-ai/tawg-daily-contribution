from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.storage import JsonlCollection


def note(title: str, body: str) -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "type: concept\n"
        "created: 2026-08-23\n"
        "updated: 2026-08-23\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def seed_retrieval(root: Path) -> None:
    knowledge = root / "knowledge"
    knowledge.mkdir()
    (knowledge / "erc-8004.md").write_text(
        note(
            "ERC-8004",
            "Trustless agents use identity, reputation, and validation registries.\n\n"
            "Open work focuses on composable validation.",
        ),
        encoding="utf-8",
    )
    (knowledge / "erc-8183.md").write_text(
        note("ERC-8183", "Agentic commerce uses escrow and evaluator settlement."),
        encoding="utf-8",
    )
    (knowledge / "zh.md").write_text(
        note("验证", "链上验证和信誉需要清晰的信任边界。"), encoding="utf-8"
    )
    timestamp = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
    source = SourceRecord.from_text(
        record_id="tg:tawg:7",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:7",
        author_person_id="alice",
        author_source_handle="Alice",
        created_at=timestamp,
        updated_at=timestamp,
        text_original="Validation registry implementation shipped.",
        ingested_at=timestamp,
    )
    source_path = root / "data/telegram/2026/08/messages.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(JsonlCollection(source_path, SourceRecord).merged_bytes([source]))


def test_build_is_deterministic_and_bm25_ranks_pages_and_sources(tmp_path: Path) -> None:
    seed_retrieval(tmp_path)
    retriever = VaultRetriever(tmp_path)

    first = retriever.build()
    index_bytes = (tmp_path / ".vault-meta/bm25.json").read_bytes()
    second = retriever.build()

    assert first == second
    assert (tmp_path / ".vault-meta/bm25.json").read_bytes() == index_bytes
    results = retriever.query("identity reputation validation", top_k=3)
    assert results[0].path == "knowledge/erc-8004.md"
    assert any(result.record_id == "tg:tawg:7" for result in results)
    assert all(result.mode == "bm25" for result in results)


def test_multilingual_tokenization_and_deterministic_chunking(tmp_path: Path) -> None:
    seed_retrieval(tmp_path)
    retriever = VaultRetriever(tmp_path, max_chunk_chars=90)

    stats = retriever.build()
    chinese = retriever.query("链上验证", top_k=2)

    assert stats.chunk_count >= 4
    assert chinese[0].path == "knowledge/zh.md"
    assert retriever.preview_chunks() == retriever.preview_chunks()


def test_missing_corrupt_or_stale_index_falls_back_to_text_search(tmp_path: Path) -> None:
    seed_retrieval(tmp_path)
    retriever = VaultRetriever(tmp_path)

    missing = retriever.query("escrow evaluator", top_k=2)
    assert missing[0].mode == "text-fallback"

    retriever.build()
    (tmp_path / ".vault-meta/bm25.json").write_text("not-json", encoding="utf-8")
    corrupt = retriever.query("escrow evaluator", top_k=2)
    assert corrupt[0].mode == "text-fallback"

    retriever.build()
    page = tmp_path / "knowledge/erc-8183.md"
    page.write_text(note("ERC-8183", "Escrow evaluator hooks changed."), encoding="utf-8")
    stale = retriever.query("escrow evaluator", top_k=2)
    assert stale[0].mode == "text-fallback"


def test_build_rejects_symlinked_derived_cache_directory(tmp_path: Path) -> None:
    seed_retrieval(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".vault-meta").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        VaultRetriever(tmp_path).build()
