from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.open_knowledge_migration import (
    MigrationConflict,
    OpenKnowledgeMigration,
)
from tawg_bot.vault import parse_frontmatter

NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)


def _page(title: str, page_type: str, body: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "created: '2026-08-23'\n"
        "updated: '2026-08-23'\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _scaffold(root: Path) -> None:
    (root / "knowledge/repos").mkdir(parents=True)
    (root / "knowledge/topics").mkdir(parents=True)
    (root / "data/state").mkdir(parents=True)
    (root / "data/telegram/2026/08").mkdir(parents=True)
    (root / "knowledge/repos/widget.md").write_text(
        _page(
            "Widget",
            "repository",
            (
                "Evidence: [README](https://github.com/trustless-ai/widget/"
                "blob/0123456789abcdef0123456789abcdef01234567/README.md)."
            ),
        ),
        encoding="utf-8",
    )
    (root / "knowledge/topics/garden-clock.md").write_text(
        _page("Garden Clock", "concept", "A retained legacy explanation."),
        encoding="utf-8",
    )
    (root / "data/telegram/2026/08/messages.jsonl").write_text(
        '{"record_id":"tg:tawg:1","text_original":"raw"}\n',
        encoding="utf-8",
    )
    jobs = [
        {
            "schema_version": "tawg.knowledge-refresh-job.v1",
            "job_key": f"refresh:erc-8183:source-{index}:{index:016x}",
            "erc_number": 8183,
            "source_key": f"source-{index}",
            "observed_sha256": f"{index + 1:064x}",
        }
        for index in range(2)
    ]
    (root / "data/state/pending-knowledge-refresh.json").write_text(
        json.dumps(jobs, indent=2) + "\n",
        encoding="utf-8",
    )


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _snapshot_bodies(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): parse_frontmatter(
            path.read_text(encoding="utf-8")
        )[1]
        for path in sorted(root.rglob("*.md"))
    }


def test_migration_preserves_raw_records_and_archives_refresh_jobs(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    telegram_before = _snapshot_tree(tmp_path / "data/telegram")
    bodies_before = _snapshot_bodies(tmp_path / "knowledge")

    summary = OpenKnowledgeMigration(tmp_path).run(now=NOW)

    assert _snapshot_tree(tmp_path / "data/telegram") == telegram_before
    assert _snapshot_bodies(tmp_path / "knowledge") == bodies_before
    assert summary.legacy_refresh_jobs_archived == 2
    assert summary.provenance_backfilled == 1
    assert summary.provenance_marked_incomplete == 1
    assert json.loads(
        (tmp_path / "data/state/pending-knowledge-refresh.json").read_text()
    ) == []
    audit = json.loads(
        (tmp_path / OpenKnowledgeMigration.STATE_PATH).read_text(encoding="utf-8")
    )
    assert len(audit["archived_refresh_jobs"]) == 2
    assert audit["schema_version"] == "tawg.open-knowledge-migration.v1"


def test_migration_backfills_repo_and_marks_topic_incomplete(tmp_path: Path) -> None:
    _scaffold(tmp_path)

    OpenKnowledgeMigration(tmp_path).run(now=NOW)

    repo_frontmatter, _ = parse_frontmatter(
        (tmp_path / "knowledge/repos/widget.md").read_text(encoding="utf-8")
    )
    topic_frontmatter, _ = parse_frontmatter(
        (tmp_path / "knowledge/topics/garden-clock.md").read_text(encoding="utf-8")
    )
    assert repo_frontmatter is not None
    assert repo_frontmatter["provenance_status"] == "verified"
    assert repo_frontmatter["source_urls"] == [
        "https://github.com/trustless-ai/widget/"
        "blob/0123456789abcdef0123456789abcdef01234567/README.md"
    ]
    assert topic_frontmatter is not None
    assert topic_frontmatter["provenance_status"] == "legacy_incomplete"


def test_migration_second_run_has_no_diff(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    migration = OpenKnowledgeMigration(tmp_path)
    migration.run(now=NOW)
    first = _snapshot_tree(tmp_path)

    summary = migration.run(now=NOW)

    assert _snapshot_tree(tmp_path) == first
    assert summary.changed is False


def test_migration_rejects_output_drift_after_completion(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    migration = OpenKnowledgeMigration(tmp_path)
    migration.run(now=NOW)
    page = tmp_path / "knowledge/topics/garden-clock.md"
    page.write_text(page.read_text() + "drift\n", encoding="utf-8")

    with pytest.raises(MigrationConflict):
        migration.run(now=NOW)


def test_migration_state_hashes_are_bound_to_published_outputs(tmp_path: Path) -> None:
    _scaffold(tmp_path)

    OpenKnowledgeMigration(tmp_path).run(now=NOW)

    audit = json.loads(
        (tmp_path / OpenKnowledgeMigration.STATE_PATH).read_text(encoding="utf-8")
    )
    for relative_path, expected in audit["output_hashes"].items():
        payload = (tmp_path / relative_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected


def test_migration_rejects_symlinked_operational_state(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    refresh = tmp_path / OpenKnowledgeMigration.REFRESH_PATH
    refresh.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("[]\n", encoding="utf-8")
    refresh.symlink_to(outside)

    with pytest.raises(MigrationConflict, match="refresh queue"):
        OpenKnowledgeMigration(tmp_path).run(now=NOW)
