"""Command-line entry points for deterministic operator tasks."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from tawg_bot.daily import PreparedDaily
from tawg_bot.telegram_export import TelegramDesktopImporter
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class OperationalRuntime(Protocol):
    async def tick(self, now: datetime, *, observe_only: bool) -> None: ...

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
    sources = commands.add_parser("check-sources")
    sources.add_argument("--erc", type=_erc_number)
    sources.add_argument("--observe-only", action="store_true")
    refresh = commands.add_parser("refresh-knowledge")
    refresh.add_argument("--erc", type=_erc_number)
    refresh.add_argument("--dry-run", action="store_true")
    daily = commands.add_parser("daily-dry-run")
    daily.add_argument("--window-end", type=_utc_timestamp, required=True)
    commands.add_parser("vault-lint")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime: OperationalRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    args = _parser().parse_args(argv)
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
    operational = runtime or _production_runtime(root)
    if args.command == "tick":
        now = args.now or (clock or (lambda: datetime.now(UTC)))()
        asyncio.run(operational.tick(now, observe_only=args.observe_only))
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


def _production_runtime(root: Path) -> OperationalRuntime:
    from tawg_bot.runtime import ProductionRuntime

    return ProductionRuntime.from_environment(root)


if __name__ == "__main__":
    raise SystemExit(main())
