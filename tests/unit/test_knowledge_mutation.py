from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.knowledge_mutation import (
    KnowledgeMutationRejected,
    build_mutation_capability,
    extract_public_https_urls,
    validate_knowledge_transaction,
)
from tawg_bot.models import BotRoute, SourceRecord, SourceType
from tawg_bot.vault_transaction import VaultTransaction

NOW = datetime(2026, 8, 28, 2, 3, tzinfo=UTC)


def _record(record_id: str, text: str) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="Alice",
        created_at=NOW,
        updated_at=NOW,
        text_original=text,
        ingested_at=NOW,
    )


def _page(title: str, body_chars: int, *, status: str = "verified") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: concept\n"
        "created: '2026-08-28'\n"
        "updated: '2026-08-28'\n"
        f"provenance_status: {status}\n"
        "source_urls:\n"
        "- https://github.com/trustless-ai/example\n"
        "---\n\n"
        f"# {title}\n\n"
        + ("x" * body_chars)
        + "\n"
    )


def _write_page(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _transaction(
    *,
    path: str,
    expected_sha256: str | None,
    citations: list[str],
    content: str = (
        "---\n"
        "title: Garden Clock\n"
        "type: concept\n"
        "created: '2026-08-28'\n"
        "updated: '2026-08-28'\n"
        "source_ids:\n"
        "- tg:tawg:5000\n"
        "---\n\n"
        "# Garden Clock\n"
    ),
) -> VaultTransaction:
    return VaultTransaction.model_validate(
        {
            "operation_id": "reply:tg:tawg:5000",
            "writes": [
                {
                    "path": path,
                    "expected_sha256": expected_sha256,
                    "content": content,
                    "citations": citations,
                }
            ],
        }
    )


def test_explicit_mention_can_create_arbitrary_knowledge(tmp_path: Path) -> None:
    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=_record("tg:tawg:5000", "Please record our Garden Clock concept."),
        reply_chain=(),
        retrieved_paths=(),
    )

    assert capability.can_create_page is True
    assert capability.allowed_create_roots == (
        "knowledge/repos",
        "knowledge/topics",
    )
    assert capability.required_evidence == ("tg:tawg:5000",)
    validate_knowledge_transaction(
        tmp_path,
        _transaction(
            path="knowledge/topics/garden-clock.md",
            expected_sha256=None,
            citations=["tg:tawg:5000"],
        ),
        capability,
    )


def test_only_public_https_urls_are_exposed_as_mutation_evidence() -> None:
    record = _record(
        "tg:tawg:5000",
        " ".join(
            (
                "https://example.org",
                "https://example.org/original.",
                "http://example.org/insecure",
                "https://localhost/private",
                "https://127.0.0.1/private",
                "https://user@example.org/private",
            )
        ),
    )

    assert extract_public_https_urls((record,)) == (
        "https://example.org",
        "https://example.org/original",
    )


def test_exact_revisions_stop_at_three_and_sixty_thousand_chars(tmp_path: Path) -> None:
    paths = tuple(f"knowledge/topics/topic-{index}.md" for index in range(4))
    for index, path in enumerate(paths):
        _write_page(tmp_path, path, _page(f"Topic {index}", 19_000))

    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=_record("tg:tawg:5000", "Update these topics."),
        reply_chain=(),
        retrieved_paths=paths,
    )

    assert len(capability.exact_revisions) == 3
    assert sum(len(item.content) for item in capability.exact_revisions) <= 60_000


def test_legacy_incomplete_page_is_not_exposed_as_exact_revision(tmp_path: Path) -> None:
    path = "knowledge/topics/legacy.md"
    _write_page(tmp_path, path, _page("Legacy", 100, status="legacy_incomplete"))

    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=_record("tg:tawg:5000", "Update legacy."),
        reply_chain=(),
        retrieved_paths=(path,),
    )

    assert capability.exact_revisions == ()


def test_stale_revision_is_rejected_without_overwrite(tmp_path: Path) -> None:
    path = "knowledge/topics/garden-clock.md"
    current = _page("Garden Clock", 100)
    _write_page(tmp_path, path, current)
    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=_record("tg:tawg:5000", "Update Garden Clock."),
        reply_chain=(),
        retrieved_paths=(path,),
    )
    expected = hashlib.sha256(current.encode()).hexdigest()
    transaction = _transaction(
        path=path,
        expected_sha256=expected,
        citations=["tg:tawg:5000"],
        content=_page("Garden Clock", 101),
    )
    _write_page(tmp_path, path, current + "drift\n")

    with pytest.raises(KnowledgeMutationRejected, match="stale knowledge revision"):
        validate_knowledge_transaction(tmp_path, transaction, capability)


@pytest.mark.parametrize(
    ("path", "citations"),
    [
        ("knowledge/ercs/new.md", ["tg:tawg:5000"]),
        ("knowledge/topics/index.md", ["tg:tawg:5000"]),
        ("knowledge/topics/new.md", []),
    ],
)
def test_new_page_requires_confined_root_and_trigger_evidence(
    tmp_path: Path,
    path: str,
    citations: list[str],
) -> None:
    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=_record("tg:tawg:5000", "Record this."),
        reply_chain=(),
        retrieved_paths=(),
    )

    with pytest.raises(KnowledgeMutationRejected):
        validate_knowledge_transaction(
            tmp_path,
            _transaction(path=path, expected_sha256=None, citations=citations),
            capability,
        )
