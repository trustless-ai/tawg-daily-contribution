"""Isolated repository checkouts for replay-safe operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, TypeVar

RepositoryErrorCode = Literal[
    "repository_checkpoint_failed",
    "repository_checkout_failed",
    "repository_command_failed",
    "repository_merge_failed",
]


class RepositoryConflict(RuntimeError):
    """An optimistic repository checkpoint conflict."""

    def __init__(self) -> None:
        super().__init__("repository_conflict")


class RepositorySessionError(RuntimeError):
    """A repository-session failure containing only a fixed safe code."""

    def __init__(self, code: RepositoryErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int


class CommandRunner(Protocol):
    async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult: ...


class AsyncioCommandRunner:
    async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except OSError:
            raise RepositorySessionError("repository_command_failed") from None
        del stdout, stderr
        return CommandResult(returncode=process.returncode or 0)


T = TypeVar("T")


class RepositorySession:
    """Run an async operation in a disposable checkout, replaying one conflict."""

    def __init__(
        self,
        *,
        remote: str,
        branch: str,
        runner: CommandRunner | None = None,
        merge_branch: str | None = None,
    ) -> None:
        self.remote = remote
        self.branch = branch
        self.runner = runner or AsyncioCommandRunner()
        self.merge_branch = merge_branch

    async def run(
        self,
        *,
        operation_id: str,
        operation: Callable[[Path], Awaitable[T]],
    ) -> T:
        for attempt in range(2):
            try:
                with TemporaryDirectory(prefix="tawg-repository-") as temporary_directory:
                    checkout = Path(temporary_directory) / "repository"
                    clone_argv = [
                        "git",
                        "clone",
                        "--branch",
                        self.branch,
                        "--single-branch",
                    ]
                    if self.merge_branch is None:
                        clone_argv.extend(("--depth", "1"))
                    clone_argv.extend(("--", self.remote, str(checkout)))
                    await self._run_command(
                        argv=tuple(clone_argv),
                        cwd=checkout.parent,
                        failure_code="repository_checkout_failed",
                    )
                    if self.merge_branch is not None and self.branch != self.merge_branch:
                        await self._run_command(
                            argv=(
                                "git",
                                "fetch",
                                "origin",
                                f"{self.merge_branch}:refs/remotes/origin/{self.merge_branch}",
                            ),
                            cwd=checkout,
                            failure_code="repository_merge_failed",
                        )
                        await self._run_command(
                            argv=(
                                "git",
                                "merge",
                                "--no-edit",
                                f"origin/{self.merge_branch}",
                            ),
                            cwd=checkout,
                            failure_code="repository_merge_failed",
                        )
                    result = await operation(checkout)
                    checkpoint = await self._run_command(
                        argv=(
                            "bash",
                            str(checkout / "scripts/commit_operation.sh"),
                            operation_id,
                        ),
                        cwd=checkout,
                        failure_code="repository_checkpoint_failed",
                        allow_conflict=True,
                    )
                    if checkpoint.returncode == 75:
                        raise RepositoryConflict
                    return result
            except RepositoryConflict:
                if attempt == 1:
                    raise
        raise AssertionError("repository session exhausted")

    async def _run_command(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        failure_code: RepositoryErrorCode,
        allow_conflict: bool = False,
    ) -> CommandResult:
        try:
            result = await self.runner.run(argv=argv, cwd=cwd)
        except RepositorySessionError:
            raise RepositorySessionError(failure_code) from None
        except Exception:
            raise RepositorySessionError(failure_code) from None
        if result.returncode != 0 and not (allow_conflict and result.returncode == 75):
            raise RepositorySessionError(failure_code)
        return result
