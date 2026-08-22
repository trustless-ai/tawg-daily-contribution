import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tawg_bot.vault import VaultLinter


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
