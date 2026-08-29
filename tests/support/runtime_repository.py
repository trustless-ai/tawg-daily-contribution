from __future__ import annotations

import shutil
from pathlib import Path

from tawg_bot.models import SourceCursors


def copy_static_runtime_tree(project_root: Path, root: Path) -> None:
    """Copy runtime code/configuration without copying mutable bot state."""

    for relative in (
        "config",
        "knowledge",
        "prompts",
        "bot-skill",
        "src/tawg_bot/schemas",
    ):
        shutil.copytree(project_root / relative, root / relative)
    initialize_empty_runtime_state(root)


def initialize_empty_runtime_state(root: Path) -> None:
    """Create only the deterministic state required by offline runtime tests."""

    state = root / "data/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "pending-bot-jobs.json").write_text("[]\n", encoding="utf-8")
    (state / "delivery-state.json").write_text("[]\n", encoding="utf-8")
    (state / "github-announcement-state.json").write_text(
        '{\n  "schema_version": "tawg.github-announcement-state.v1",\n'
        '  "initialized_at": "2026-08-01T00:00:00Z",\n'
        '  "repositories": []\n}\n',
        encoding="utf-8",
    )
    (state / "pending-github-announcements.json").write_text(
        "[]\n", encoding="utf-8"
    )
    (state / "source-cursors.json").write_text(
        SourceCursors().model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
