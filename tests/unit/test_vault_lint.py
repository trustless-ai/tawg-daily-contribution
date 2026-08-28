import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tawg_bot.vault import VaultLinter, frontmatter_is_mutation_evidence


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


def seed_meta(root: Path) -> None:
    meta = root / "knowledge/meta"
    meta.mkdir(parents=True)
    (meta / "source-ledger.json").write_text(
        '{"schema":"tawg.source-ledger.v1","entries":{}}\n', encoding="utf-8"
    )
    (meta / "claim-ledger.json").write_text(
        '{"schema":"tawg.claim-ledger.v1","entries":{}}\n', encoding="utf-8"
    )


def test_lint_reports_frontmatter_dead_ambiguous_links_and_orphans(tmp_path: Path) -> None:
    seed_meta(tmp_path)
    knowledge = tmp_path / "knowledge"
    (knowledge / "index.md").write_text(
        note("Index", "[[alpha/target]] [[target]] [[missing]]"), encoding="utf-8"
    )
    (knowledge / "alpha").mkdir()
    (knowledge / "beta").mkdir()
    (knowledge / "alpha/target.md").write_text(note("Target A", "Content."), encoding="utf-8")
    (knowledge / "beta/target.md").write_text(note("Target B", "Content."), encoding="utf-8")
    (knowledge / "orphan.md").write_text(note("Orphan", "Content."), encoding="utf-8")
    (knowledge / "broken.md").write_text("# No frontmatter\n", encoding="utf-8")

    report = VaultLinter(tmp_path).lint()
    categories = {finding.category for finding in report.findings}

    assert {"frontmatter", "dead_link", "ambiguous_link", "orphan"}.issubset(categories)
    assert report.error_count == 3


def test_lint_reports_invalid_ledgers_and_stale_support(tmp_path: Path) -> None:
    seed_meta(tmp_path)
    knowledge = tmp_path / "knowledge"
    (knowledge / "index.md").write_text(note("Index", "No links yet."), encoding="utf-8")
    now = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=1)
    source = {
        "source_id": "tg:tawg:1",
        "authority": "primary",
        "independence_key": "telegram-thread",
        "active": True,
        "fresh_until": stale.isoformat(),
    }
    (knowledge / "meta/source-ledger.json").write_text(
        json.dumps({"schema": "tawg.source-ledger.v1", "entries": {"tg:tawg:1": source}}),
        encoding="utf-8",
    )
    claim = {
        "claim_id": "claim:one",
        "state": "accepted",
        "risk": "ordinary",
        "source_ids": ["tg:tawg:1"],
        "assessed_at": now.isoformat(),
    }
    (knowledge / "meta/claim-ledger.json").write_text(
        json.dumps({"schema": "tawg.claim-ledger.v1", "entries": {"claim:one": claim}}),
        encoding="utf-8",
    )

    report = VaultLinter(tmp_path).lint(now=now)

    assert any(finding.category == "stale_support" for finding in report.findings)

    (knowledge / "meta/claim-ledger.json").write_text("[]", encoding="utf-8")
    invalid = VaultLinter(tmp_path).lint(now=now)
    assert any(finding.category == "invalid_ledger" for finding in invalid.findings)


def test_lint_accepts_v2_metadata_ledgers(tmp_path: Path) -> None:
    seed_meta(tmp_path)
    knowledge = tmp_path / "knowledge"
    (knowledge / "index.md").write_text(note("Index", "Generated knowledge."), encoding="utf-8")
    now = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
    source = {
        "source_kind": "normative_spec",
        "authority": "canonical",
        "canonical_url": "https://eips.ethereum.org/EIPS/eip-8004",
        "observed_version": "v1",
        "observed_sha256": "0" * 64,
        "observed_at": now.isoformat(),
        "independence_key": "eips.ethereum.org",
        "active": True,
    }
    (knowledge / "meta/source-ledger.json").write_text(
        json.dumps(
            {
                "schema": "tawg.source-ledger.v2",
                "entries": {"erc-8004-canonical": source},
            }
        ),
        encoding="utf-8",
    )
    claim = {
        "claim_kind": "normative",
        "state": "accepted",
        "risk": "ordinary",
        "source_keys": ["erc-8004-canonical"],
        "assessed_at": now.isoformat(),
    }
    (knowledge / "meta/claim-ledger.json").write_text(
        json.dumps(
            {
                "schema": "tawg.claim-ledger.v2",
                "entries": {"erc-8004-interface": claim},
            }
        ),
        encoding="utf-8",
    )

    report = VaultLinter(tmp_path).lint(now=now)

    assert not [finding for finding in report.findings if finding.severity == "error"]


def test_legacy_incomplete_page_is_navigation_but_not_mutation_evidence() -> None:
    assert (
        frontmatter_is_mutation_evidence(
            {
                "title": "Legacy topic",
                "type": "concept",
                "created": "2026-08-23",
                "updated": "2026-08-23",
                "provenance_status": "legacy_incomplete",
            }
        )
        is False
    )
    assert (
        frontmatter_is_mutation_evidence(
            {
                "title": "Verified topic",
                "type": "concept",
                "created": "2026-08-23",
                "updated": "2026-08-23",
                "provenance_status": "verified",
                "source_urls": ["https://github.com/trustless-ai/example"],
            }
        )
        is True
    )


@pytest.mark.parametrize("legacy_directory", ["people", "People", "PEOPLE"])
def test_lint_rejects_legacy_people_directory(tmp_path: Path, legacy_directory: str) -> None:
    seed_meta(tmp_path)
    knowledge = tmp_path / "knowledge"
    (knowledge / "index.md").write_text(note("Index", "[[people/alice]]"), encoding="utf-8")
    legacy = knowledge / legacy_directory
    legacy.mkdir()
    (legacy / "alice.md").write_text(note("Alice", "Public contribution."), encoding="utf-8")

    report = VaultLinter(tmp_path).lint()

    assert any(
        finding.category == "legacy_acknowledgement_path"
        and finding.path == f"knowledge/{legacy_directory}/alice.md"
        and finding.severity == "error"
        for finding in report.findings
    )
