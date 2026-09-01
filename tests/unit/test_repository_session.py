from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest

from tawg_bot.repository_session import (
    CommandResult,
    RepositoryConflict,
    RepositorySession,
    RepositorySessionError,
)


class FakeRunner:
    def __init__(
        self,
        result_for: Callable[[Sequence[str]], CommandResult | Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.result_for = result_for or (lambda argv: CommandResult(returncode=0))

    async def run(self, *, argv: Sequence[str], cwd: Path) -> CommandResult:
        self.calls.append((tuple(argv), cwd))
        result = self.result_for(argv)
        if isinstance(result, Exception):
            raise result
        if result.returncode == 0 and tuple(argv[:2]) == ("git", "clone"):
            Path(argv[-1]).mkdir()
        return result


async def _return_root(root: Path) -> Path:
    assert root.exists()
    return root


@pytest.mark.asyncio
async def test_run_uses_fresh_branch_checkout_and_restricted_checkpoint() -> None:
    runner = FakeRunner()
    remote = "ssh://git@example.invalid/tawg/repository.git"
    session = RepositorySession(remote=remote, branch="main", runner=runner)

    checkout = await session.run(operation_id="webhook:123", operation=_return_root)

    clone_argv, clone_cwd = runner.calls[0]
    checkout_root = Path(clone_argv[-1])
    assert clone_argv == (
        "git",
        "clone",
        "--branch",
        "main",
        "--single-branch",
        "--depth",
        "1",
        "--",
        remote,
        str(checkout_root),
    )
    assert clone_cwd == checkout_root.parent
    assert runner.calls[1] == (
        ("bash", str(checkout_root / "scripts/commit_operation.sh"), "webhook:123"),
        checkout_root,
    )
    assert checkout == checkout_root
    assert not checkout_root.exists()


@pytest.mark.asyncio
async def test_checkpoint_conflict_replays_once_in_a_new_checkout() -> None:
    checkpoint_calls = 0

    def result_for(argv: Sequence[str]) -> CommandResult:
        nonlocal checkpoint_calls
        if argv[0] == "bash":
            checkpoint_calls += 1
            return CommandResult(returncode=75 if checkpoint_calls == 1 else 0)
        return CommandResult(returncode=0)

    runner = FakeRunner(result_for)
    operation_roots: list[Path] = []

    async def operation(root: Path) -> str:
        if operation_roots:
            assert not operation_roots[0].exists()
        operation_roots.append(root)
        return root.name

    result = await RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="main",
        runner=runner,
    ).run(operation_id="maintenance:123", operation=operation)

    assert result == operation_roots[1].name
    assert len(operation_roots) == 2
    assert operation_roots[0] != operation_roots[1]
    assert all(not root.exists() for root in operation_roots)
    assert sum(argv[:2] == ("git", "clone") for argv, _cwd in runner.calls) == 2


@pytest.mark.asyncio
async def test_operation_conflict_replays_once_then_propagates() -> None:
    runner = FakeRunner()
    roots: list[Path] = []

    async def conflict(root: Path) -> None:
        roots.append(root)
        raise RepositoryConflict

    with pytest.raises(RepositoryConflict):
        await RepositorySession(
            remote="https://example.invalid/tawg/repository.git",
            branch="main",
            runner=runner,
        ).run(operation_id="webhook:456", operation=conflict)

    assert len(roots) == 2
    assert roots[0] != roots[1]
    assert all(not root.exists() for root in roots)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (CommandResult(returncode=2), "repository_checkout_failed"),
        (OSError("https://token@example.invalid secret stderr"), "repository_checkout_failed"),
    ],
)
async def test_checkout_failure_reports_only_a_fixed_safe_code(
    failure: CommandResult | Exception,
    expected_code: str,
) -> None:
    runner = FakeRunner(lambda argv: failure)
    operation: Callable[[Path], Awaitable[Path]] = _return_root

    with pytest.raises(RepositorySessionError) as captured:
        await RepositorySession(
            remote="https://credential@example.invalid/tawg/repository.git",
            branch="main",
            runner=runner,
        ).run(operation_id="webhook:789", operation=operation)

    assert captured.value.code == expected_code
    assert str(captured.value) == expected_code
    assert "credential" not in str(captured.value)
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_non_conflict_checkpoint_failure_is_not_retried() -> None:
    def result_for(argv: Sequence[str]) -> CommandResult:
        return CommandResult(returncode=1 if argv[0] == "bash" else 0)

    runner = FakeRunner(result_for)
    roots: list[Path] = []

    async def operation(root: Path) -> None:
        roots.append(root)

    with pytest.raises(RepositorySessionError) as captured:
        await RepositorySession(
            remote="https://example.invalid/tawg/repository.git",
            branch="main",
            runner=runner,
        ).run(operation_id="webhook:999", operation=operation)

    assert captured.value.code == "repository_checkpoint_failed"
    assert len(roots) == 1
    assert not roots[0].exists()


@pytest.mark.asyncio
async def test_merge_branch_fetches_and_merges_before_operation() -> None:
    runner = FakeRunner()
    session = RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="dev",
        runner=runner,
        merge_branch="main",
    )

    await session.run(operation_id="webhook:42", operation=_return_root)

    commands = [argv for argv, _cwd in runner.calls]
    assert ("git", "fetch", "origin", "main:refs/remotes/origin/main") in commands
    assert ("git", "merge", "--no-edit", "origin/main") in commands
    # fetch/merge must run after clone and before the checkpoint
    clone_index = next(i for i, argv in enumerate(commands) if argv[:2] == ("git", "clone"))
    assert "--depth" not in commands[clone_index]
    fetch_index = next(i for i, argv in enumerate(commands) if argv[:2] == ("git", "fetch"))
    merge_index = next(i for i, argv in enumerate(commands) if argv[:2] == ("git", "merge"))
    bash_index = next(i for i, argv in enumerate(commands) if argv[:1] == ("bash",))
    assert clone_index < fetch_index < merge_index < bash_index


@pytest.mark.asyncio
async def test_merge_conflict_fails_closed_with_merge_error() -> None:
    def result_for(argv: Sequence[str]) -> CommandResult:
        return CommandResult(returncode=1 if argv[:2] == ("git", "merge") else 0)

    runner = FakeRunner(result_for)
    roots: list[Path] = []

    async def operation(root: Path) -> None:
        roots.append(root)

    with pytest.raises(RepositorySessionError) as captured:
        await RepositorySession(
            remote="https://example.invalid/tawg/repository.git",
            branch="dev",
            runner=runner,
            merge_branch="main",
        ).run(operation_id="webhook:43", operation=operation)

    assert captured.value.code == "repository_merge_failed"
    assert roots == []


@pytest.mark.asyncio
async def test_no_merge_when_branch_equals_merge_branch() -> None:
    runner = FakeRunner()
    session = RepositorySession(
        remote="https://example.invalid/tawg/repository.git",
        branch="main",
        runner=runner,
        merge_branch="main",
    )

    await session.run(operation_id="webhook:44", operation=_return_root)

    commands = [argv for argv, _cwd in runner.calls]
    assert not any(argv[:2] == ("git", "fetch") for argv in commands)
    assert not any(argv[:2] == ("git", "merge") for argv in commands)
