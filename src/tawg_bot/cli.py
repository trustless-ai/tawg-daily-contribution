"""Command-line entry points for deterministic operator tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from tawg_bot.daily import PreparedDaily
from tawg_bot.telegram_export import TelegramDesktopImporter
from tawg_bot.telegram_webhook import MAX_BODY_BYTES, TelegramWebhookEnvelope
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class OperationalRuntime(Protocol):
    async def tick(self, now: datetime, *, observe_only: bool) -> None: ...

    async def maintenance_tick(self, now: datetime, *, observe_only: bool) -> None: ...

    async def ingest_webhook_envelope(
        self, envelope: TelegramWebhookEnvelope, *, now: datetime
    ) -> object: ...

    async def check_sources(self, erc: int | None, *, observe_only: bool) -> object: ...

    async def refresh_knowledge(self, erc: int | None, *, dry_run: bool) -> object: ...

    async def daily_dry_run(self, window_end: datetime) -> PreparedDaily | None: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tawg-bot")
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("import-telegram-history")
    history.add_argument("--input", type=Path, required=True)
    history.add_argument("--group-slug", default="tawg")
    history.add_argument("--dry-run", action="store_true")
    tick = commands.add_parser("tick")
    tick.add_argument("--now", type=_utc_timestamp)
    tick.add_argument("--observe-only", action="store_true")
    maintenance = commands.add_parser("maintenance-tick")
    maintenance.add_argument("--now", type=_utc_timestamp)
    maintenance.add_argument("--observe-only", action="store_true")
    webhook = commands.add_parser("ingest-webhook-envelope")
    webhook.add_argument("--input", required=True)
    webhook.add_argument("--now", type=_utc_timestamp)
    sources = commands.add_parser("check-sources")
    sources.add_argument("--erc", type=_erc_number)
    sources.add_argument("--observe-only", action="store_true")
    refresh = commands.add_parser("refresh-knowledge")
    refresh.add_argument("--erc", type=_erc_number)
    refresh.add_argument("--dry-run", action="store_true")
    daily = commands.add_parser("daily-dry-run")
    daily.add_argument("--window-end", type=_utc_timestamp, required=True)
    migration = commands.add_parser("migrate-open-knowledge")
    migration.add_argument("--now", type=_utc_timestamp)
    commands.add_parser("vault-lint")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime: OperationalRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "import-telegram-history":
        importer = TelegramDesktopImporter.for_repository(root)
        if args.dry_run:
            import_report = importer.parse(args.input, group_slug=args.group_slug)
            changed: tuple[str, ...] = ()
        else:
            uow = RepositoryUnitOfWork(root, operation_id=f"history-{uuid4()}")
            import_report = importer.import_file(args.input, group_slug=args.group_slug, uow=uow)
            changed = uow.publish().changed_paths
        print(
            f"imported={import_report.imported} rejected={import_report.rejected} "
            f"changed_paths={len(changed)} dry_run={args.dry_run}"
        )
        return 0
    if args.command == "vault-lint":
        lint_report = VaultLinter(root).lint()
        print(f"errors={lint_report.error_count} warnings={lint_report.warning_count}")
        return 1 if lint_report.error_count else 0
    if args.command == "migrate-open-knowledge":
        from tawg_bot.open_knowledge_migration import OpenKnowledgeMigration

        now = args.now or (clock or (lambda: datetime.now(UTC)))()
        migration_summary = OpenKnowledgeMigration(root).run(now=now)
        print(
            f"archived_refresh_jobs={migration_summary.legacy_refresh_jobs_archived} "
            f"provenance_backfilled={migration_summary.provenance_backfilled} "
            f"provenance_incomplete={migration_summary.provenance_marked_incomplete} "
            f"scan_targets_seeded={migration_summary.scan_targets_seeded} "
            f"changed={migration_summary.changed}"
        )
        return 0
    operational = runtime or _production_runtime(root)
    if args.command == "tick":
        now = args.now or (clock or (lambda: datetime.now(UTC)))()
        asyncio.run(operational.tick(now, observe_only=args.observe_only))
        return 0
    if args.command == "maintenance-tick":
        now = args.now or (clock or (lambda: datetime.now(UTC)))()
        asyncio.run(operational.maintenance_tick(now, observe_only=args.observe_only))
        return 0
    if args.command == "ingest-webhook-envelope":
        try:
            envelope = _read_webhook_envelope(args.input, root=root)
        except (OSError, UnicodeError, ValueError):
            parser.error("webhook envelope input is invalid")
        now = args.now or (clock or (lambda: datetime.now(UTC)))()
        result = asyncio.run(operational.ingest_webhook_envelope(envelope, now=now))
        print(
            f"received={getattr(result, 'received', 0)} "
            f"persisted={getattr(result, 'persisted', 0)} "
            f"replayed={getattr(result, 'replayed', 0)} "
            f"jobs_created={getattr(result, 'jobs_created', 0)}"
        )
        return 0
    if args.command == "check-sources":
        summary = asyncio.run(operational.check_sources(args.erc, observe_only=args.observe_only))
        print(
            f"ercs={getattr(summary, 'erc_count', 0)} "
            f"evidence={getattr(summary, 'evidence_count', 0)} "
            f"gaps={getattr(summary, 'gap_count', 0)} "
            f"refresh_jobs={getattr(summary, 'refresh_job_count', 0)} "
            f"persisted={getattr(summary, 'persisted', False)}"
        )
        return 0
    if args.command == "refresh-knowledge":
        result = asyncio.run(operational.refresh_knowledge(args.erc, dry_run=args.dry_run))
        print(
            f"processed_jobs={len(getattr(result, 'processed_job_keys', ()))} "
            f"changed_paths={len(getattr(result, 'changed_paths', ()))} "
            f"index_rebuilt={getattr(result, 'index_rebuilt', False)} "
            f"dry_run={args.dry_run}"
        )
        return 0
    if args.command == "daily-dry-run":
        prepared = asyncio.run(operational.daily_dry_run(args.window_end))
        if prepared is None:
            print("No Daily content was prepared.")
        else:
            print(prepared.telegram_text)
        return 0
    raise AssertionError("unreachable command")


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 UTC") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("timestamp must use UTC")
    return parsed.astimezone(UTC)


def _erc_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ERC number must be an integer") from None
    if not 1 <= number <= 99_999:
        raise argparse.ArgumentTypeError("ERC number must be between 1 and 99999")
    return number


def _read_webhook_envelope(value: str, *, root: Path) -> TelegramWebhookEnvelope:
    if value == "-":
        raw = sys.stdin.read(MAX_BODY_BYTES + 1)
    else:
        path = (root / value).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("webhook envelope path is invalid")
        if path.stat().st_size > MAX_BODY_BYTES:
            raise ValueError("webhook envelope input is too large")
        raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("webhook envelope input is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("webhook envelope input must contain one object")
    return TelegramWebhookEnvelope.model_validate(payload)


def _production_runtime(root: Path) -> OperationalRuntime:
    from tawg_bot.runtime import ProductionRuntime

    return ProductionRuntime.from_environment(root)


if __name__ == "__main__":
    raise SystemExit(main())
