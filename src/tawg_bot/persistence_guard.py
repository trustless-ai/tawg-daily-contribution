"""Final, content-aware policy boundary for repository persistence."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

from tawg_bot.source_registry import RegistryRejected, SourceRegistry


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
_REFRESH_STATE_PATH = "data/state/pending-knowledge-refresh.json"
_SOURCE_METADATA_NAMES = frozenset(
    {
        "aliases.yml",
        "claim-ledger.json",
        "scan-targets.yml",
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

    def __init__(
        self,
        external_texts: tuple[str, ...] = (),
        *,
        source_registry_baseline: str | None = None,
        trusted_source_locators: Iterable[str] = (),
    ) -> None:
        self._external_texts = tuple(
            normalized for value in external_texts if (normalized := _normalize(value))
        )
        self._trusted_source_locators = frozenset(
            _trusted_source_locator(value) for value in trusted_source_locators
        )
        if source_registry_baseline is None:
            self._source_registry_baseline: SourceRegistry | None = None
        else:
            try:
                self._source_registry_baseline = SourceRegistry.from_yaml_text(
                    _normalize(source_registry_baseline)
                )
            except RegistryRejected:
                raise PersistenceRejected from None

    @classmethod
    def from_external_texts(
        cls,
        values: Iterable[str],
        *,
        source_registry_baseline: str | None = None,
        trusted_source_locators: Iterable[str] = (),
    ) -> PersistenceGuard:
        return cls(
            tuple(values),
            source_registry_baseline=source_registry_baseline,
            trusted_source_locators=trusted_source_locators,
        )

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
            for value, allow_short_quote in _semantic_strings(
                relative_path,
                text,
                expected,
                source_registry_baseline=self._source_registry_baseline,
                trusted_source_locators=self._trusted_source_locators,
            ):
                if self._contains_external_excerpt(value, allow_short_quote):
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


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _trusted_source_locator(value: str) -> str:
    normalized = _normalize(value)
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
    ):
        raise PersistenceRejected
    return normalized


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
    *,
    source_registry_baseline: SourceRegistry | None,
    trusted_source_locators: frozenset[str],
) -> tuple[tuple[str, bool], ...]:
    path = PurePosixPath(relative_path)
    if provenance is PersistenceProvenance.GENERATED_KNOWLEDGE:
        return ((text, True),)
    if relative_path == "knowledge/meta/sources.yml":
        if source_registry_baseline is None:
            raise PersistenceRejected
        try:
            registry = SourceRegistry.from_yaml_text(text)
            if text != _normalize(registry.render_with_observations({})):
                raise RegistryRejected("source registry is not canonically rendered")
            updated_versions = registry.updated_versions_from(source_registry_baseline)
        except RegistryRejected:
            raise PersistenceRejected from None
        return tuple((_normalize(version), False) for version in updated_versions)
    if relative_path == "knowledge/meta/scan-targets.yml":
        from tawg_bot.scan_targets import ScanTargetRegistry, ScanTargetRejected

        try:
            scan_registry = ScanTargetRegistry.from_yaml_text(text)
            if text != _normalize(scan_registry.render_yaml()):
                raise ScanTargetRejected("scan target registry is not canonically rendered")
        except ScanTargetRejected:
            raise PersistenceRejected from None
        return ()
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
    if relative_path == _REFRESH_STATE_PATH and source_registry_baseline is not None:
        return tuple(_walk_refresh_job_strings(value, source_registry_baseline))
    if relative_path == "data/state/prepared-daily.json":
        return tuple(
            _walk_prepared_daily_strings(
                value,
                trusted_source_locators=trusted_source_locators,
            )
        )
    if relative_path == "data/state/pending-bot-jobs.json":
        return tuple(
            _walk_prepared_reply_job_strings(
                value,
                trusted_source_locators=trusted_source_locators,
            )
        )
    return tuple(_walk_strings(value, allowed_key=allowed_key))


def _walk_prepared_daily_strings(
    value: Any,
    *,
    trusted_source_locators: frozenset[str],
) -> Iterable[tuple[str, bool]]:
    if not isinstance(value, Mapping):
        yield from _walk_strings(value, allowed_key="telegram_text")
        return
    for key, child in value.items():
        normalized_key = _normalize(str(key))
        yield (normalized_key, False)
        if normalized_key == "citations" and isinstance(child, list):
            for citation in child:
                if (
                    isinstance(citation, str)
                    and _normalize(citation) in trusted_source_locators
                ):
                    continue
                yield from _walk_strings(citation, allowed_key=None)
            continue
        if isinstance(child, str):
            yield (_normalize(child), normalized_key == "telegram_text")
        else:
            yield from _walk_strings(child, allowed_key="telegram_text")


def _walk_prepared_reply_job_strings(
    value: Any,
    *,
    trusted_source_locators: frozenset[str],
) -> Iterable[tuple[str, bool]]:
    if not isinstance(value, list):
        yield from _walk_strings(value, allowed_key="prepared_reply_text")
        return
    for item in value:
        if not isinstance(item, Mapping):
            yield from _walk_strings(item, allowed_key="prepared_reply_text")
            continue
        for key, child in item.items():
            normalized_key = _normalize(str(key))
            yield (normalized_key, False)
            if normalized_key == "prepared_citations" and isinstance(child, list):
                for citation in child:
                    if (
                        isinstance(citation, str)
                        and _normalize(citation) in trusted_source_locators
                    ):
                        continue
                    yield from _walk_strings(citation, allowed_key=None)
                continue
            if isinstance(child, str):
                yield (_normalize(child), normalized_key == "prepared_reply_text")
            else:
                yield from _walk_strings(child, allowed_key="prepared_reply_text")


def _walk_refresh_job_strings(
    value: Any,
    registry: SourceRegistry,
) -> Iterable[tuple[str, bool]]:
    if not isinstance(value, list):
        yield from _walk_strings(value, allowed_key=None)
        return
    for item in value:
        if not isinstance(item, Mapping):
            yield from _walk_strings(item, allowed_key=None)
            continue
        trusted_source_key, trusted_job_key = _trusted_refresh_fields(item, registry)
        for key, child in item.items():
            normalized_key = _normalize(str(key))
            yield (normalized_key, False)
            if not isinstance(child, str):
                yield from _walk_strings(child, allowed_key=None)
                continue
            normalized_child = _normalize(child)
            if normalized_key == "source_key" and normalized_child == trusted_source_key:
                continue
            if normalized_key == "job_key" and normalized_child == trusted_job_key:
                continue
            yield (normalized_child, False)


def _trusted_refresh_fields(
    item: Mapping[Any, Any],
    registry: SourceRegistry,
) -> tuple[str | None, str | None]:
    source_key = item.get("source_key")
    erc_number = item.get("erc_number")
    observed_sha256 = item.get("observed_sha256")
    job_key = item.get("job_key")
    if (
        not isinstance(source_key, str)
        or not isinstance(erc_number, int)
        or isinstance(erc_number, bool)
        or not 1 <= erc_number <= 99_999
        or not isinstance(observed_sha256, str)
        or len(observed_sha256) != 64
        or any(character not in "0123456789abcdef" for character in observed_sha256)
        or not isinstance(job_key, str)
    ):
        return None, None
    try:
        source = registry.source(source_key)
    except KeyError:
        return None, None
    if f"erc-{erc_number}" not in source.topics:
        return None, None
    expected_job_key = f"refresh:erc-{erc_number}:{source_key}:{observed_sha256[:16]}"
    if job_key != expected_job_key:
        return None, None
    return _normalize(source_key), _normalize(job_key)


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
