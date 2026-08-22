"""Command-line entry points for deterministic operator tasks."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from tawg_bot.telegram_export import TelegramDesktopImporter
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class OperationalRuntime(Protocol):
    async def tick(self, now: datetime, *, observe_only: bool) -> None: ...

    async def backfill(self, source: str) -> None: ...

    async def daily_dry_run(self, window_end: datetime) -> None: ...


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
    backfill = commands.add_parser("backfill")
    backfill.add_argument("source", choices=("github", "magicians"))
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
            import_report = importer.import_file(
                args.input, group_slug=args.group_slug, uow=uow
            )
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
    if args.command == "backfill":
        asyncio.run(operational.backfill(args.source))
        return 0
    if args.command == "daily-dry-run":
        asyncio.run(operational.daily_dry_run(args.window_end))
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


def _production_runtime(root: Path) -> OperationalRuntime:
    from tawg_bot.runtime import ProductionRuntime

    return ProductionRuntime.from_environment(root)


if __name__ == "__main__":
    raise SystemExit(main())
