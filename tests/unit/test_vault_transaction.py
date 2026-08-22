import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection
from tawg_bot.vault_transaction import (
    ApprovalMismatch,
    TransactionRejected,
    VaultTransaction,
    VaultTransactionEngine,
)


def sha(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def page(title: str, body: str, *, source_id: str = "tg:tawg:1") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "type: concept\n"
        "created: 2026-08-23\n"
        "updated: 2026-08-23\n"
        "source_ids:\n"
        f'  - "{source_id}"\n'
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def seed_project(root: Path) -> str:
    config = root / "config"
    knowledge = root / "knowledge"
    data = root / "data/telegram/2026/08"
    config.mkdir()
    knowledge.mkdir()
    data.mkdir(parents=True)
    source_root = Path(__file__).parents[2]
    (config / "privacy.yml").write_bytes((source_root / "config/privacy.yml").read_bytes())
    (knowledge / "meta").mkdir()
    (knowledge / "meta/source-ledger.json").write_text(
        '{"schema":"tawg.source-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    (knowledge / "meta/claim-ledger.json").write_text(
        '{"schema":"tawg.claim-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    current = page("ERC-8004", "Current evidence.")
    (knowledge / "erc-8004.md").write_text(current, encoding="utf-8")
    timestamp = datetime(2026, 8, 22, 23, 0, tzinfo=UTC)
    record = SourceRecord.from_text(
        record_id="tg:tawg:1",
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:1",
        author_person_id="alice",
        author_source_handle="Alice",
        created_at=timestamp,
        updated_at=timestamp,
        text_original="Evidence for ERC-8004.",
        ingested_at=timestamp,
    )
    source_path = data / "messages.jsonl"
    source_path.write_bytes(JsonlCollection(source_path, SourceRecord).merged_bytes([record]))
    return current


def transaction(path: str, expected: str | None, content: str, citations=None):
    return VaultTransaction.model_validate(
        {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": "knowledge-refresh",
            "writes": [
                {
                    "path": path,
                    "expected_sha256": expected,
                    "content": content,
                    "citations": ["tg:tawg:1"] if citations is None else citations,
                }
            ],
        }
    )


def test_inspect_hash_binds_root_transaction_and_expected_hash_then_apply(tmp_path: Path) -> None:
    current = seed_project(tmp_path)
    updated = page("ERC-8004", "Current evidence, clarified.")
    change = transaction("knowledge/erc-8004.md", sha(current), updated)
    engine = VaultTransactionEngine(tmp_path)

    inspection = engine.inspect(change)

    assert inspection.changed_paths == ("knowledge/erc-8004.md",)
    with pytest.raises(ApprovalMismatch):
        engine.apply(change, "0" * 64)
    applied = engine.apply(change, inspection.approval_sha256)
    assert applied.changed_paths == ("knowledge/erc-8004.md",)
    assert (tmp_path / "knowledge/erc-8004.md").read_text() == updated


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/tmp/escape.md",
        "knowledge/../config/sources.yml",
        "config/sources.yml",
        ".github/workflows/bot.yml",
        "contracts/Workflow.sol",
        "skills/TAWG.md",
        "bot-skill/SKILL.md",
        "data/telegram/messages.jsonl",
        "knowledge/.vault-meta/index.json",
    ],
)
def test_rejects_paths_outside_canonical_knowledge(tmp_path: Path, unsafe_path: str) -> None:
    seed_project(tmp_path)
    change = transaction(unsafe_path, None, page("Unsafe", "No."))

    with pytest.raises(TransactionRejected):
        VaultTransactionEngine(tmp_path).inspect(change)


def test_rejects_hash_mismatch_unknown_or_missing_citations_and_private_data(
    tmp_path: Path,
) -> None:
    seed_project(tmp_path)
    engine = VaultTransactionEngine(tmp_path)

    bad_changes = [
        transaction("knowledge/erc-8004.md", "0" * 64, page("ERC-8004", "Wrong hash.")),
        transaction(
            "knowledge/new.md",
            None,
            page("New", "Unsupported.", source_id="missing:1"),
            ["missing:1"],
        ),
        transaction(
            "knowledge/new.md",
            None,
            page("New", "Hidden unsupported citation.", source_id="missing:extra"),
            ["tg:tawg:1"],
        ),
        transaction("knowledge/new.md", None, page("New", "Missing citation."), []),
        transaction(
            "knowledge/new.md",
            None,
            page("New", "Email private@example.com must not pass."),
        ),
    ]
    for change in bad_changes:
        with pytest.raises(TransactionRejected):
            engine.inspect(change)


def test_rejects_broken_wikilinks_case_collisions_and_symlink_escape(tmp_path: Path) -> None:
    seed_project(tmp_path)
    engine = VaultTransactionEngine(tmp_path)
    (tmp_path / "knowledge/People").mkdir()
    existing = page("Alice", "Known.")
    (tmp_path / "knowledge/People/Alice.md").write_text(existing, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "knowledge/link").symlink_to(outside, target_is_directory=True)

    changes = [
        transaction("knowledge/new.md", None, page("New", "See [[missing]].")),
        transaction("knowledge/people/alice.md", None, page("Alice", "Collision.")),
        transaction("knowledge/link/escape.md", None, page("Escape", "No.")),
    ]
    for change in changes:
        with pytest.raises(TransactionRejected):
            engine.inspect(change)


def test_schema_and_engine_enforce_write_count_and_total_size(tmp_path: Path) -> None:
    seed_project(tmp_path)
    with pytest.raises(ValidationError):
        VaultTransaction.model_validate(
            {
                "schema_version": "tawg.vault-transaction.v1",
                "operation_id": "too-many",
                "writes": [
                    {
                        "path": f"knowledge/{index}.md",
                        "expected_sha256": None,
                        "content": page(str(index), "Body."),
                        "citations": ["tg:tawg:1"],
                    }
                    for index in range(65)
                ],
            }
        )
    oversized = VaultTransaction.model_validate(
        {
            "schema_version": "tawg.vault-transaction.v1",
            "operation_id": "too-large",
            "writes": [
                {
                    "path": f"knowledge/large-{index}.md",
                    "expected_sha256": None,
                    "content": page(f"Large {index}", "x" * 524_200),
                    "citations": ["tg:tawg:1"],
                }
                for index in range(2)
            ],
        }
    )
    with pytest.raises(TransactionRejected):
        VaultTransactionEngine(tmp_path).inspect(oversized)


def test_transaction_json_schema_matches_model_contract() -> None:
    schema_path = Path(__file__).parents[2] / "src/tawg_bot/schemas/vault-transaction.v1.json"
    schema = json.loads(schema_path.read_text())

    assert schema["properties"]["writes"]["maxItems"] == 64
    assert "expected_sha256" in schema["properties"]["writes"]["items"]["required"]
