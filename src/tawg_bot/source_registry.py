"""Validated metadata-only registry for controller-owned live evidence."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from tawg_bot.models import StrictModel

_URL_PATH = re.compile(r"^/(?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})*$")
_MIME_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


class RegistryRejected(ValueError):
    """Raised when external evidence metadata violates the registry contract."""


class EvidenceKind(StrEnum):
    NORMATIVE_SPEC = "normative_spec"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    EXAMPLE = "example"
    DISCUSSION = "discussion"


class EvidenceAuthority(StrEnum):
    CANONICAL = "canonical"
    OFFICIAL_ORG = "official_org"
    MAINTAINER = "maintainer"
    COMMUNITY = "community"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    DISABLED = "disabled"


class SourceObservation(StrictModel):
    checked_at: datetime
    version: str | None = Field(default=None, min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_count: int = Field(ge=0, le=10_485_760)

    @field_validator("checked_at")
    @classmethod
    def checked_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("source observation timestamp must use UTC")
        return value.astimezone(UTC)


class FetchPolicy(StrictModel):
    policy: Literal["public-text"]
    allowed_hosts: list[str] = Field(min_length=1, max_length=8)
    allowed_path_prefixes: list[str] = Field(min_length=1, max_length=16)
    max_bytes: int = Field(ge=1, le=10_485_760)
    mime_types: list[str] = Field(min_length=1, max_length=16)

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_public_names(cls, values: list[str]) -> list[str]:
        normalized = [value.casefold().rstrip(".") for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed hosts must be unique")
        for host in normalized:
            if (
                not host
                or host == "example.invalid"
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in host
                )
            ):
                raise ValueError("allowed host is invalid")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            raise ValueError("IP literals are not valid registry hosts")
        return normalized

    @field_validator("allowed_path_prefixes")
    @classmethod
    def paths_are_absolute(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("allowed path prefixes must be unique")
        for value in values:
            if len(value) > 1024 or _URL_PATH.fullmatch(value) is None or ".." in value.split("/"):
                raise ValueError("allowed path prefix is invalid")
        return values

    @field_validator("mime_types")
    @classmethod
    def mimes_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [value.casefold().strip() for value in values]
        if len(normalized) != len(set(normalized)) or any(
            _MIME_TYPE.fullmatch(value) is None for value in normalized
        ):
            raise ValueError("MIME allowlist is invalid")
        return normalized

    def accepts_url(self, value: str) -> bool:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            return False
        host = parsed.hostname.casefold().rstrip(".")
        if host not in self.allowed_hosts or host == "example.invalid":
            return False
        path = parsed.path or "/"
        if len(path) > 1024 or _URL_PATH.fullmatch(path) is None:
            return False
        return any(
            path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for prefix in self.allowed_path_prefixes
        )


class RegisteredSource(StrictModel):
    source_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,127}$")
    topics: list[str] = Field(min_length=1, max_length=32)
    kind: EvidenceKind
    authority: EvidenceAuthority
    canonical_url: str = Field(min_length=1, max_length=2048)
    immutable_url: str | None = Field(default=None, min_length=1, max_length=2048)
    fetch_policy: FetchPolicy
    last_observed: SourceObservation | None = None
    status: SourceStatus

    @field_validator("topics")
    @classmethod
    def topics_are_normalized(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not value.startswith("erc-") or not value[4:].isdigit() for value in values
        ):
            raise ValueError("source topics must be unique normalized ERC topics")
        return values

    @model_validator(mode="after")
    def urls_follow_fetch_policy(self) -> RegisteredSource:
        for url in (self.canonical_url, self.immutable_url):
            if url is not None and not self.fetch_policy.accepts_url(url):
                raise ValueError("source URL is outside its fetch policy")
        return self


class _RegistryDocument(StrictModel):
    schema_version: Literal["tawg.sources.v2"] = Field(alias="schema")
    sources: list[RegisteredSource] = Field(min_length=1, max_length=2048)


_KIND_RANK = {
    EvidenceKind.NORMATIVE_SPEC: 0,
    EvidenceKind.IMPLEMENTATION: 1,
    EvidenceKind.TEST: 2,
    EvidenceKind.EXAMPLE: 2,
    EvidenceKind.DISCUSSION: 3,
}
_AUTHORITY_RANK = {
    EvidenceAuthority.CANONICAL: 0,
    EvidenceAuthority.OFFICIAL_ORG: 1,
    EvidenceAuthority.MAINTAINER: 2,
    EvidenceAuthority.COMMUNITY: 3,
}


class SourceRegistry:
    def __init__(self, document: _RegistryDocument) -> None:
        self._document = document
        self._by_key: dict[str, RegisteredSource] = {}
        for source in document.sources:
            if source.source_key in self._by_key:
                raise RegistryRejected(f"duplicate source key: {source.source_key}")
            self._by_key[source.source_key] = source
        self._validate_canonical_sources()

    @classmethod
    def from_yaml(cls, path: Path) -> SourceRegistry:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RegistryRejected("invalid source registry") from error
        return cls.from_yaml_text(text)

    @classmethod
    def from_yaml_text(cls, text: str) -> SourceRegistry:
        try:
            raw = yaml.safe_load(text)
            if not isinstance(raw, dict):
                raise ValueError("registry root must be a mapping")
            return cls(_RegistryDocument.model_validate(raw))
        except RegistryRejected:
            raise
        except (UnicodeError, ValueError, ValidationError, yaml.YAMLError) as error:
            raise RegistryRejected("invalid source registry") from error

    def resolve(
        self, erc_number: int, kinds: frozenset[EvidenceKind]
    ) -> tuple[RegisteredSource, ...]:
        if not 1 <= erc_number <= 99_999:
            raise ValueError("ERC number must be between 1 and 99999")
        topic = f"erc-{erc_number}"
        sources = [
            source
            for source in self._document.sources
            if source.status is SourceStatus.ACTIVE
            and topic in source.topics
            and source.kind in kinds
        ]
        sources.sort(
            key=lambda source: (
                _KIND_RANK[source.kind],
                _AUTHORITY_RANK[source.authority],
                source.source_key,
            )
        )
        return tuple(sources)

    def source(self, source_key: str) -> RegisteredSource:
        try:
            return self._by_key[source_key]
        except KeyError:
            raise KeyError(f"unknown source key: {source_key}") from None

    def erc_numbers(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    int(topic.removeprefix("erc-"))
                    for source in self._document.sources
                    if source.status is SourceStatus.ACTIVE
                    for topic in source.topics
                }
            )
        )

    def due_erc_numbers(self, now: datetime, *, max_age: timedelta) -> tuple[int, ...]:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("source recheck time must use UTC")
        if max_age <= timedelta(0):
            raise ValueError("source recheck age must be positive")
        cutoff = now - max_age
        return tuple(
            erc_number
            for erc_number in self.erc_numbers()
            if any(
                source.last_observed is None or source.last_observed.checked_at <= cutoff
                for source in self.resolve(erc_number, frozenset(EvidenceKind))
            )
        )

    def updated_versions_from(self, baseline: SourceRegistry) -> tuple[str, ...]:
        current_definitions = [
            source.model_dump(mode="json", exclude={"last_observed"})
            for source in self._document.sources
        ]
        baseline_definitions = [
            source.model_dump(mode="json", exclude={"last_observed"})
            for source in baseline._document.sources
        ]
        if current_definitions != baseline_definitions:
            raise RegistryRejected("source definitions cannot change during observation updates")

        updated_versions: list[str] = []
        for current, previous in zip(
            self._document.sources,
            baseline._document.sources,
            strict=True,
        ):
            observation = current.last_observed
            previous_observation = previous.last_observed
            if (
                observation != previous_observation
                and observation is not None
                and observation.version is not None
                and observation.version
                != (previous_observation.version if previous_observation is not None else None)
            ):
                updated_versions.append(observation.version)
        return tuple(updated_versions)

    def render_with_observations(self, observations: Mapping[str, SourceObservation]) -> str:
        unknown = set(observations) - set(self._by_key)
        if unknown:
            raise RegistryRejected(f"unknown source observation: {sorted(unknown)[0]}")
        updated = [
            source.model_copy(
                update={"last_observed": observations.get(source.source_key, source.last_observed)}
            )
            for source in self._document.sources
        ]
        payload = self._document.model_copy(update={"sources": updated}).model_dump(
            mode="json", by_alias=True
        )
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    def _validate_canonical_sources(self) -> None:
        by_topic: dict[str, list[RegisteredSource]] = {}
        for source in self._document.sources:
            if (
                source.status is SourceStatus.ACTIVE
                and source.kind is EvidenceKind.NORMATIVE_SPEC
                and source.authority is EvidenceAuthority.CANONICAL
            ):
                for topic in source.topics:
                    by_topic.setdefault(topic, []).append(source)
        for topic, sources in by_topic.items():
            if len(sources) > 1:
                raise RegistryRejected(f"multiple canonical sources for {topic}")
        for required in ("erc-8004", "erc-8183"):
            if not by_topic.get(required):
                raise RegistryRejected(f"missing canonical source for {required}")
