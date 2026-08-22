"""Deterministic inclusion policy for public GitHub repository files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class PathDecision:
    include_record: bool
    include_body: bool
    reason: str


class GitHubPathPolicy:
    _EXCLUDED_PARTS = frozenset(
        {
            ".git",
            ".next",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "artifacts",
            "build",
            "cache",
            "coverage",
            "dist",
            "deps",
            "generated",
            "node_modules",
            "out",
            "target",
            "vendor",
        }
    )
    _BINARY_SUFFIXES = frozenset(
        {
            ".7z",
            ".a",
            ".avi",
            ".bin",
            ".class",
            ".dll",
            ".dylib",
            ".eot",
            ".exe",
            ".gif",
            ".gz",
            ".ico",
            ".jar",
            ".jpeg",
            ".jpg",
            ".mov",
            ".mp3",
            ".mp4",
            ".o",
            ".otf",
            ".pdf",
            ".png",
            ".pyc",
            ".so",
            ".tar",
            ".tgz",
            ".ttf",
            ".wav",
            ".webm",
            ".woff",
            ".woff2",
            ".zip",
        }
    )
    _LOCK_NAMES = frozenset(
        {
            "bun.lock",
            "bun.lockb",
            "cargo.lock",
            "composer.lock",
            "foundry.lock",
            "gemfile.lock",
            "go.sum",
            "package-lock.json",
            "pnpm-lock.yaml",
            "poetry.lock",
            "uv.lock",
            "yarn.lock",
        }
    )

    def __init__(self, *, max_body_bytes: int = 512 * 1024) -> None:
        self.max_body_bytes = max_body_bytes

    def classify(self, path: str, size: int | None) -> PathDecision:
        normalized = PurePosixPath(path)
        lowered_parts = tuple(part.casefold() for part in normalized.parts)
        if any(part in self._EXCLUDED_PARTS for part in lowered_parts[:-1]):
            return PathDecision(False, False, "excluded_directory")
        name = normalized.name.casefold()
        if name in self._LOCK_NAMES or name.endswith(".lock"):
            return PathDecision(True, False, "lockfile_body_excluded")
        if normalized.suffix.casefold() in self._BINARY_SUFFIXES:
            return PathDecision(False, False, "binary")
        if size is not None and size > self.max_body_bytes:
            return PathDecision(True, False, "body_too_large")
        return PathDecision(True, True, "included")
