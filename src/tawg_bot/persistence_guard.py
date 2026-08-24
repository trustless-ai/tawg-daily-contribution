"""Final, content-aware policy boundary for repository persistence."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

import yaml


class PersistenceRejected(ValueError):
    """A deliberately non-descriptive persistence policy failure."""

    def __init__(self) -> None:
        super().__init__("persistence policy rejection")


class PersistenceProvenance(StrEnum):
    """Why a staged artifact is permitted to exist in the repository."""

    TELEGRAM_HISTORY = "telegram_history"
    GENERATED_KNOWLEDGE = "generated_knowledge"
    SOURCE_METADATA = "source_metadata"
    OPERATIONAL_STATE = "operational_state"
    PREPARED_TELEGRAM = "prepared_telegram"
    EXTERNAL_EVIDENCE = "external_evidence"


_PREPARED_TELEGRAM_PATHS = frozenset(
    {
        "data/state/pending-bot-jobs.json",
        "data/state/prepared-daily.json",
    }
)
_SOURCE_METADATA_NAMES = frozenset(
    {
        "aliases.yml",
        "claim-ledger.json",
        "source-ledger.json",
        "sources.yml",
    }
)
_EXTERNAL_DATA_ROOTS = (
    PurePosixPath("data/github"),
    PurePosixPath("data/magicians"),
)


class PersistenceGuard:
    """Validate staged paths and reject copied transient evidence before disk publication."""

    _FORBIDDEN_EXCERPT_CHARS = 16
    _GENERATED_EXCERPT_CHARS = 96

    def __init__(self, external_texts: tuple[str, ...] = ()) -> None:
        self._external_texts = tuple(
            normalized for value in external_texts if (normalized := _normalize(value))
        )

    @classmethod
    def from_external_texts(cls, values: Iterable[str]) -> PersistenceGuard:
        return cls(tuple(values))

    @staticmethod
    def provenance_for_path(relative_path: str) -> PersistenceProvenance:
        path = _confined_path(relative_path)
        if _under(path, PurePosixPath("data/telegram")) and path.suffix == ".jsonl":
            return PersistenceProvenance.TELEGRAM_HISTORY
        if relative_path in _PREPARED_TELEGRAM_PATHS:
            return PersistenceProvenance.PREPARED_TELEGRAM
        if _under(path, PurePosixPath("data/state")) and path.suffix == ".json":
            return PersistenceProvenance.OPERATIONAL_STATE
        if path.parent == PurePosixPath("knowledge/meta") and path.name in _SOURCE_METADATA_NAMES:
            return PersistenceProvenance.SOURCE_METADATA
        if _under(path, PurePosixPath("knowledge/meta")):
            raise PersistenceRejected
        if _under(path, PurePosixPath("knowledge")) and path.suffix == ".md":
            return PersistenceProvenance.GENERATED_KNOWLEDGE
        raise PersistenceRejected

    def inspect_staged(
        self,
        payloads: Mapping[str, bytes],
        provenance: Mapping[str, PersistenceProvenance],
    ) -> None:
        if set(payloads) != set(provenance):
            raise PersistenceRejected
        for relative_path, payload in payloads.items():
            expected = self.provenance_for_path(relative_path)
            if provenance[relative_path] is not expected:
                raise PersistenceRejected
            path = _confined_path(relative_path)
            if any(_under(path, root) for root in _EXTERNAL_DATA_ROOTS):
                raise PersistenceRejected
            try:
                text = _normalize(payload.decode("utf-8"))
            except UnicodeDecodeError:
                raise PersistenceRejected from None
            for value, allow_short_quote in _semantic_strings(relative_path, text, expected):
                if self._contains_external_excerpt(value, allow_short_quote):
                    _debug_rejection(relative_path, allow_short_quote, self._external_texts, value)
                    raise PersistenceRejected

    def _contains_external_excerpt(self, payload: str, allow_short_quote: bool) -> bool:
        if not payload or not self._external_texts:
            return False
        minimum = (
            self._GENERATED_EXCERPT_CHARS if allow_short_quote else self._FORBIDDEN_EXCERPT_CHARS
        )
        for source in self._external_texts:
            if not allow_short_quote and source in payload:
                return True
            if len(source) < minimum or len(payload) < minimum:
                continue
            if _shares_window(source, payload, minimum):
                return True
        return False


def _longest_shared(source: str, payload: str, minimum: int) -> int:
    source_bytes = source.encode("utf-8")
    payload_bytes = payload.encode("utf-8")
    if len(source_bytes) < minimum or len(payload_bytes) < minimum:
        return 0
    lo, hi, best = minimum, min(len(source_bytes), len(payload_bytes)), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _shares_window_bytes(source_bytes, payload_bytes, mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _shares_window_bytes(source_bytes: bytes, payload_bytes: bytes, width: int) -> bool:
    payload_hashes = _rolling_hashes(payload_bytes, width)
    return any(value in payload_hashes for value in _rolling_hashes(source_bytes, width))


def _first_shared_window(source: str, payload: str, width: int) -> str | None:
    source_bytes = source.encode("utf-8")
    payload_bytes = payload.encode("utf-8")
    payload_hashes = _rolling_hashes(payload_bytes, width)
    for value, source_offsets in _rolling_hashes(source_bytes, width).items():
        candidates = payload_hashes.get(value)
        if candidates is None:
            continue
        for offset in source_offsets:
            chunk = source_bytes[offset : offset + width]
            if any(payload_bytes[start : start + width] == chunk for start in candidates):
                return chunk.decode("utf-8", errors="replace")
    return None


def _debug_rejection(path: str, allow_short_quote: bool, sources: tuple[str, ...], payload: str) -> None:
    try:
        import sys
        minimum = 96 if allow_short_quote else 16
        matches = []
        for index, source in enumerate(sources):
            shared = _longest_shared(source, payload, minimum)
            if shared >= minimum:
                matches.append((index, shared, _first_shared_window(source, payload, shared)))
        print(
            f"PERSISTENCE-DEBUG path={path} allow_short={allow_short_quote} "
            f"sources={len(sources)} matches={len(matches)}",
            file=sys.stderr,
        )
        for index, shared, chunk in matches[:3]:
            print(
                f"PERSISTENCE-DEBUG source[{index}] longest_shared={shared} chunk={chunk!r}",
                file=sys.stderr,
            )
    except Exception:
        import sys
        print(f"PERSISTENCE-DEBUG path={path} allow_short={allow_short_quote} longest_shared=ERR", file=sys.stderr)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _confined_path(relative_path: str) -> PurePosixPath:
    if "\\" in relative_path or any(ord(character) < 32 for character in relative_path):
        raise PersistenceRejected
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() != relative_path
    ):
        raise PersistenceRejected
    return path


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _semantic_strings(
    relative_path: str,
    text: str,
    provenance: PersistenceProvenance,
) -> tuple[tuple[str, bool], ...]:
    path = PurePosixPath(relative_path)
    if provenance is PersistenceProvenance.GENERATED_KNOWLEDGE:
        return ((text, True),)
    try:
        if path.suffix == ".json":
            value = json.loads(text)
        elif path.suffix in {".yml", ".yaml"}:
            value = yaml.load(text, Loader=yaml.BaseLoader)
        elif path.suffix == ".jsonl":
            value = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            raise PersistenceRejected
    except (json.JSONDecodeError, yaml.YAMLError):
        raise PersistenceRejected from None
    allowed_key: str | None = None
    if provenance is PersistenceProvenance.PREPARED_TELEGRAM:
        allowed_key = (
            "telegram_text"
            if relative_path == "data/state/prepared-daily.json"
            else "prepared_reply_text"
        )
    return tuple(_walk_strings(value, allowed_key=allowed_key))


def _walk_strings(value: Any, *, allowed_key: str | None) -> Iterable[tuple[str, bool]]:
    if isinstance(value, str):
        yield (_normalize(value), False)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = _normalize(str(key))
            yield (normalized_key, False)
            if isinstance(child, str):
                yield (_normalize(child), normalized_key == allowed_key)
            else:
                yield from _walk_strings(child, allowed_key=allowed_key)
        return
    if isinstance(value, list):
        for child in value:
            yield from _walk_strings(child, allowed_key=allowed_key)


def _shares_window(source: str, payload: str, width: int) -> bool:
    """Return whether two normalized strings share an exact fixed-width span."""
    source_bytes = source.encode("utf-8")
    payload_bytes = payload.encode("utf-8")
    if len(source_bytes) < width or len(payload_bytes) < width:
        return False
    payload_hashes = _rolling_hashes(payload_bytes, width)
    for value, source_offsets in _rolling_hashes(source_bytes, width).items():
        candidates = payload_hashes.get(value)
        if candidates is None:
            continue
        for offset in source_offsets:
            chunk = source_bytes[offset : offset + width]
            if any(payload_bytes[start : start + width] == chunk for start in candidates):
                return True
    return False


def _rolling_hashes(payload: bytes, width: int) -> dict[int, list[int]]:
    base = 257
    mask = (1 << 64) - 1
    power = pow(base, width - 1, 1 << 64)
    value = 0
    for item in payload[:width]:
        value = ((value * base) + item) & mask
    hashes: dict[int, list[int]] = {value: [0]}
    for offset in range(1, len(payload) - width + 1):
        outgoing = payload[offset - 1]
        incoming = payload[offset + width - 1]
        value = (((value - (outgoing * power)) * base) + incoming) & mask
        hashes.setdefault(value, []).append(offset)
    return hashes
