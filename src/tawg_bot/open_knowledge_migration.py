"""One-time, hash-bound migration into the open knowledge storage contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import parse_frontmatter

_TRUSTLESS_AI_URL = re.compile(
    r"https://github\.com/trustless-ai/[A-Za-z0-9_.-]+(?:/[^\s)\]>]*)?"
)


class MigrationConflict(RuntimeError):
    """Raised when a completed migration's hash-bound outputs have drifted."""


@dataclass(frozen=True, slots=True)
class MigrationSummary:
    legacy_refresh_jobs_archived: int
    provenance_backfilled: int
    provenance_marked_incomplete: int
    scan_targets_seeded: int
    changed: bool


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("migration time must use UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _render_page(frontmatter: dict[str, Any], body: str) -> bytes:
    rendered = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{rendered}---\n{body}".encode()


class OpenKnowledgeMigration:
    """Preserve raw evidence while migrating legacy operational metadata once."""

    VERSION = "open-knowledge-v1"
    STATE_PATH = "data/state/migrations/open-knowledge-v1.json"
    REFRESH_PATH = "data/state/pending-knowledge-refresh.json"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(self, *, now: datetime) -> MigrationSummary:
        completed_at = _utc_text(now)
        state_path = self.root / self.STATE_PATH
        if state_path.exists():
            if state_path.is_symlink() or not state_path.is_file():
                raise MigrationConflict("completed migration state is invalid")
            return self._completed_summary(state_path)

        refresh_path = self.root / self.REFRESH_PATH
        if refresh_path.exists():
            if refresh_path.is_symlink() or not refresh_path.is_file():
                raise MigrationConflict("legacy refresh queue is invalid")
            refresh_payload = refresh_path.read_bytes()
        else:
            refresh_payload = b"[]\n"
        try:
            refresh_jobs = json.loads(refresh_payload)
        except json.JSONDecodeError as error:
            raise MigrationConflict("legacy refresh queue is invalid") from error
        if not isinstance(refresh_jobs, list):
            raise MigrationConflict("legacy refresh queue is invalid")

        inputs: dict[str, bytes] = {self.REFRESH_PATH: refresh_payload}
        outputs: dict[str, bytes] = {self.REFRESH_PATH: b"[]\n"}
        backfilled = 0
        incomplete = 0
        for path in self._legacy_knowledge_pages():
            relative = path.relative_to(self.root).as_posix()
            original = path.read_bytes()
            inputs[relative] = original
            migrated, status = self._migrate_page(original)
            outputs[relative] = migrated
            if status == "verified":
                backfilled += 1
            elif status == "legacy_incomplete":
                incomplete += 1

        input_hashes = {path: _sha256(payload) for path, payload in sorted(inputs.items())}
        output_hashes = {path: _sha256(payload) for path, payload in sorted(outputs.items())}
        state = {
            "schema_version": "tawg.open-knowledge-migration.v1",
            "migration_version": self.VERSION,
            "completed_at": completed_at,
            "input_hashes": input_hashes,
            "output_hashes": output_hashes,
            "legacy_refresh_jobs_archived": len(refresh_jobs),
            "provenance_backfilled": backfilled,
            "provenance_marked_incomplete": incomplete,
            "scan_targets_seeded": 0,
            "archived_refresh_jobs": refresh_jobs,
        }
        state_payload = (
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        uow = RepositoryUnitOfWork(self.root, operation_id=self.VERSION)
        uow.register_external_evidence(())
        for relative, payload in sorted(outputs.items()):
            if inputs[relative] != payload:
                uow.stage_bytes(relative, payload)
        uow.stage_bytes(self.STATE_PATH, state_payload)
        published = uow.publish()
        return MigrationSummary(
            legacy_refresh_jobs_archived=len(refresh_jobs),
            provenance_backfilled=backfilled,
            provenance_marked_incomplete=incomplete,
            scan_targets_seeded=0,
            changed=bool(published.changed_paths),
        )

    def _completed_summary(self, state_path: Path) -> MigrationSummary:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict)
                or state.get("schema_version") != "tawg.open-knowledge-migration.v1"
                or state.get("migration_version") != self.VERSION
                or not isinstance(state.get("output_hashes"), dict)
            ):
                raise ValueError("invalid migration state")
            output_hashes = state["output_hashes"]
            for relative, expected in output_hashes.items():
                if not isinstance(relative, str) or not isinstance(expected, str):
                    raise ValueError("invalid migration hashes")
                path = self.root / relative
                if not path.is_file() or _sha256(path.read_bytes()) != expected:
                    raise MigrationConflict("completed migration output has drifted")
            return MigrationSummary(
                legacy_refresh_jobs_archived=int(
                    state["legacy_refresh_jobs_archived"]
                ),
                provenance_backfilled=int(state["provenance_backfilled"]),
                provenance_marked_incomplete=int(
                    state["provenance_marked_incomplete"]
                ),
                scan_targets_seeded=int(state["scan_targets_seeded"]),
                changed=False,
            )
        except MigrationConflict:
            raise
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise MigrationConflict("completed migration state is invalid") from error

    def _legacy_knowledge_pages(self) -> tuple[Path, ...]:
        pages: list[Path] = []
        for directory in ("repos", "topics"):
            root = self.root / "knowledge" / directory
            if not root.exists():
                continue
            for path in sorted(root.glob("*.md")):
                if path.is_file() and not path.is_symlink():
                    pages.append(path)
        return tuple(pages)

    @staticmethod
    def _migrate_page(payload: bytes) -> tuple[bytes, str | None]:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationConflict("legacy knowledge page is not UTF-8") from error
        frontmatter, body = parse_frontmatter(text)
        if frontmatter is None:
            raise MigrationConflict("legacy knowledge page has invalid frontmatter")
        existing_status = frontmatter.get("provenance_status")
        if existing_status is not None:
            if existing_status not in {"verified", "legacy_incomplete"}:
                raise MigrationConflict("legacy knowledge page has invalid provenance")
            return payload, None

        urls = sorted(set(_TRUSTLESS_AI_URL.findall(body)))
        telegram_ids = [
            value
            for value in frontmatter.get("source_ids", [])
            if isinstance(value, str) and value.startswith("tg:")
        ]
        migrated = dict(frontmatter)
        if len(urls) == 1:
            migrated["source_urls"] = urls
            if telegram_ids:
                migrated["telegram_record_ids"] = telegram_ids
            migrated["provenance_status"] = "verified"
            status = "verified"
        else:
            if telegram_ids:
                migrated["telegram_record_ids"] = telegram_ids
            migrated["provenance_status"] = "legacy_incomplete"
            status = "legacy_incomplete"
        return _render_page(migrated, body), status
