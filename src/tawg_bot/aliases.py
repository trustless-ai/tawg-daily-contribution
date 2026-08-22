"""TAWG-local, human-readable identity aliases."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


class AliasError(ValueError):
    """Base error for unsafe or uncertain identity operations."""


class AmbiguousAlias(AliasError):
    """Raised when an identity lookup has more than one valid answer."""


class InvalidAliasScope(AliasError):
    """Raised when an alias attempts to escape the local TAWG namespace."""


class AliasRegistry:
    _SOURCES = frozenset({"telegram", "github", "magicians"})
    _PERSON_ID = re.compile(r"^[^\s:/\\]{1,128}$", re.UNICODE)

    def __init__(self, people: dict[str, dict[str, Any]] | None = None) -> None:
        self.people = people or {}
        self._session_keys: dict[str, str] = {}
        self._validate_and_migrate()

    @classmethod
    def from_yaml(cls, path: Path) -> AliasRegistry:
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "tawg.aliases.v1"
            or raw.get("scope") != "tawg-only"
            or not isinstance(raw.get("people", {}), dict)
        ):
            raise InvalidAliasScope("invalid TAWG alias registry")
        return cls(dict(raw.get("people", {})))

    def resolve_telegram_export(self, *, transient_key: str, display_name: str) -> str:
        if transient_key in self._session_keys:
            return self._session_keys[transient_key]
        base = self._slug(display_name)
        person_id = self._first_available_alias(base, display_name, reuse_display=True)
        self._add_display_name(person_id, display_name)
        self._session_keys[transient_key] = person_id
        return person_id

    def resolve_telegram_live(self, *, public_username: str | None, display_name: str) -> str:
        if public_username:
            return self.resolve_public_handle(
                source="telegram",
                public_handle=public_username,
                display_name=display_name,
            )
        return self.resolve_telegram_export(
            transient_key=f"name:{self._normalize_name(display_name)}",
            display_name=display_name,
        )

    def resolve_public_handle(
        self, *, source: str, public_handle: str, display_name: str
    ) -> str:
        normalized_source = self._source(source)
        normalized_handle = self._normalize_handle(public_handle)
        existing = self.lookup_public_handle(normalized_source, normalized_handle)
        if existing is not None:
            self._add_display_name(existing, display_name)
            return existing
        person_id = self._first_available_alias(
            self._slug(display_name), display_name, reuse_display=False
        )
        self._add_display_name(person_id, display_name)
        self.add_public_handle(
            person_id,
            source=normalized_source,
            public_handle=normalized_handle,
        )
        return person_id

    def add_public_handle(self, person_id: str, *, source: str, public_handle: str) -> None:
        self._require_person(person_id)
        normalized_source = self._source(source)
        normalized_handle = self._normalize_handle(public_handle)
        owner = self.lookup_public_handle(normalized_source, normalized_handle)
        if owner is not None and owner != person_id:
            raise AmbiguousAlias(f"handle already belongs to {owner}")
        identity = self.people.setdefault(person_id, {"display_names": [], "handles": {}})
        handles = identity.setdefault("handles", {})
        source_handles = handles.setdefault(normalized_source, [])
        if normalized_handle not in source_handles:
            source_handles.append(normalized_handle)
            source_handles.sort()

    def lookup_public_handle(self, source: str, public_handle: str) -> str | None:
        normalized_source = self._source(source)
        normalized_handle = self._normalize_handle(public_handle)
        matches = [
            person_id
            for person_id, identity in self.people.items()
            if normalized_handle
            in self._identity_handles(identity).get(normalized_source, [])
        ]
        if len(matches) > 1:
            raise AmbiguousAlias("public handle is assigned more than once")
        return matches[0] if matches else None

    def lookup_display_name(self, display_name: str) -> str | None:
        normalized = self._normalize_name(display_name)
        matches = [
            person_id
            for person_id, identity in self.people.items()
            if any(
                self._normalize_name(name) == normalized
                for name in identity.get("display_names", [])
                if isinstance(name, str)
            )
        ]
        if len(matches) > 1:
            raise AmbiguousAlias("display name matches multiple TAWG-local people")
        return matches[0] if matches else None

    def merge(self, primary_person_id: str, secondary_person_id: str) -> None:
        self._require_person(primary_person_id)
        self._require_person(secondary_person_id)
        if primary_person_id == secondary_person_id:
            return
        primary = self.people[primary_person_id]
        secondary = self.people[secondary_person_id]
        secondary_handles = self._identity_handles(secondary)
        for source, handles in secondary_handles.items():
            for handle in handles:
                owner = self.lookup_public_handle(source, handle)
                if owner not in {primary_person_id, secondary_person_id}:
                    raise AmbiguousAlias(f"handle already belongs to {owner}")
        for display_name in secondary.get("display_names", []):
            if isinstance(display_name, str):
                self._add_display_name(primary_person_id, display_name)
        del self.people[secondary_person_id]
        for source, handles in secondary_handles.items():
            for handle in handles:
                self.add_public_handle(primary_person_id, source=source, public_handle=handle)
        self._session_keys = {
            key: primary_person_id if value == secondary_person_id else value
            for key, value in self._session_keys.items()
        }
        self.people[primary_person_id] = primary

    def to_yaml_bytes(self) -> bytes:
        return yaml.safe_dump(
            {"schema": "tawg.aliases.v1", "scope": "tawg-only", "people": self.people},
            allow_unicode=True,
            sort_keys=True,
        ).encode()

    def _first_available_alias(
        self, base: str, display_name: str, *, reuse_display: bool
    ) -> str:
        allocated = set(self._session_keys.values())
        suffix = 1
        while True:
            candidate = base if suffix == 1 else f"{base}-{suffix}"
            suffix += 1
            if candidate in allocated:
                continue
            existing = self.people.get(candidate)
            if existing is None:
                return candidate
            if reuse_display and display_name in existing.get("display_names", []):
                return candidate

    def _add_display_name(self, person_id: str, display_name: str) -> None:
        identity = self.people.setdefault(person_id, {"display_names": [], "handles": {}})
        names = identity.setdefault("display_names", [])
        if display_name not in names:
            names.append(display_name)
            names.sort()

    def _validate_and_migrate(self) -> None:
        handle_owners: dict[tuple[str, str], str] = {}
        for person_id, identity in self.people.items():
            if (
                not isinstance(person_id, str)
                or not self._PERSON_ID.fullmatch(person_id)
                or not isinstance(identity, dict)
            ):
                raise InvalidAliasScope("person IDs must stay inside the TAWG-local namespace")
            names = identity.setdefault("display_names", [])
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise InvalidAliasScope("display_names must be strings")
            legacy_handles = identity.pop("public_handles", [])
            handles = identity.setdefault("handles", {})
            if not isinstance(handles, dict):
                raise InvalidAliasScope("handles must be source-scoped")
            if isinstance(legacy_handles, list) and legacy_handles:
                telegram_handles = handles.setdefault("telegram", [])
                for handle in legacy_handles:
                    if isinstance(handle, str) and handle not in telegram_handles:
                        telegram_handles.append(self._normalize_handle(handle))
            for source, source_handles in handles.items():
                self._source(source)
                if not isinstance(source_handles, list) or not all(
                    isinstance(handle, str) for handle in source_handles
                ):
                    raise InvalidAliasScope("public handles must be strings")
                handles[source] = sorted(
                    {self._normalize_handle(handle) for handle in source_handles}
                )
                for handle in handles[source]:
                    key = (source, handle)
                    owner = handle_owners.get(key)
                    if owner is not None and owner != person_id:
                        raise AmbiguousAlias(
                            f"{source} handle {handle} belongs to both {owner} and {person_id}"
                        )
                    handle_owners[key] = person_id

    def _require_person(self, person_id: str) -> None:
        if not self._PERSON_ID.fullmatch(person_id) or person_id not in self.people:
            raise InvalidAliasScope("unknown or non-local person ID")

    @classmethod
    def _source(cls, value: str) -> str:
        if not isinstance(value, str):
            raise InvalidAliasScope("identity source must be text")
        source = value.casefold()
        if source not in cls._SOURCES:
            raise InvalidAliasScope("identity source is outside the TAWG alias registry")
        return source

    @staticmethod
    def _identity_handles(identity: dict[str, Any]) -> dict[str, list[str]]:
        handles = identity.get("handles", {})
        return handles if isinstance(handles, dict) else {}

    @staticmethod
    def _normalize_handle(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidAliasScope("public handle must be text")
        handle = value.casefold().strip().lstrip("@")
        if not handle or len(handle) > 128 or any(character.isspace() for character in handle):
            raise InvalidAliasScope("public handle is invalid")
        return handle

    @staticmethod
    def _normalize_name(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^\w-]+", "-", value.casefold(), flags=re.UNICODE).strip("-_")
        return slug or "member"
