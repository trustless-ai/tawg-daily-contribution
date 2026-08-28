"""Deterministic privacy gate for every public and model-facing payload."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PrivacyViolation(ValueError):
    """Raised when content is unsafe for a public boundary."""


class PrivateChatRejected(PrivacyViolation):
    """Raised when a Telegram private chat reaches the public pipeline."""


class SensitiveContentRejected(PrivacyViolation):
    """Raised when a payload contains secret material."""


@dataclass(frozen=True, slots=True)
class PrivacyResult:
    accepted: bool
    sanitized_text: str | None
    reason_code: str | None

    def safe_failure_json(self, *, source_id: str) -> str:
        if self.accepted or self.reason_code is None:
            raise ValueError("an accepted result has no failure record")
        return json.dumps(
            {
                "schema": "tawg.privacy-rejection.v1",
                "source_id": source_id,
                "reason_code": self.reason_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class PrivacyFilter:
    _EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
    _PHONE = re.compile(r"(?<!\w)(?!\d{4}-\d{2}-\d{2}\b)\+?\d[\d ()-]{7,}\d(?!\w)")
    _IPV4 = re.compile(
        r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
    )
    _IPV6 = re.compile(r"(?<![\w:])(?:[A-F0-9]{0,4}:){2,7}[A-F0-9]{0,4}(?![\w:])", re.I)
    _LOCAL_PATH = re.compile(
        r"(?:/(?:Users|home|private|var/folders)/[^\s]+|[A-Z]:\\(?:Users|Documents)\\[^\s]+)",
        re.I,
    )
    _WALLET = re.compile(r"(?<![A-Fa-f0-9])0x[A-Fa-f0-9]{40}(?![A-Fa-f0-9])")
    _TELEGRAM_TOKEN = re.compile(r"(?<!\w)\d{6,12}:[A-Za-z0-9_-]{30,}(?!\w)")
    _PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", re.I)
    _API_TOKEN = re.compile(r"(?<!\w)(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{20,}(?!\w)", re.I)
    _AWS_ACCESS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
    _SLACK_TOKEN = re.compile(r"(?<!\w)xox[baprs]-[A-Za-z0-9-]{10,}(?!\w)", re.I)
    _SEED_PHRASE = re.compile(
        r"\b(?:seed|recovery|mnemonic) phrase\s*:\s*(?:[a-z]+\s+){11,23}[a-z]+\b",
        re.I,
    )
    _IDENTITY_CONTAINERS = frozenset({"from", "sender", "user", "actor"})

    def __init__(
        self,
        *,
        public_wallet_allowlist: frozenset[str],
        drop_numeric_id_keys: frozenset[str],
        drop_path_keys: frozenset[str],
        private_chat_types: frozenset[str],
    ) -> None:
        self._public_wallet_allowlist = public_wallet_allowlist
        self._drop_numeric_id_keys = drop_numeric_id_keys
        self._drop_path_keys = drop_path_keys
        self._private_chat_types = private_chat_types

    @classmethod
    def from_yaml(cls, path: Path) -> PrivacyFilter:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != "tawg.privacy.v1":
            raise ValueError("unsupported privacy configuration")
        return cls(
            public_wallet_allowlist=frozenset(raw.get("public_wallet_allowlist", [])),
            drop_numeric_id_keys=frozenset(raw.get("drop_numeric_id_keys", [])),
            drop_path_keys=frozenset(raw.get("drop_path_keys", [])),
            private_chat_types=frozenset(raw.get("private_chat_types", [])),
        )

    def inspect(self, text: str) -> PrivacyResult:
        for pattern in (
            self._TELEGRAM_TOKEN,
            self._PRIVATE_KEY,
            self._API_TOKEN,
            self._AWS_ACCESS_KEY,
            self._SLACK_TOKEN,
            self._SEED_PHRASE,
        ):
            if pattern.search(text):
                return PrivacyResult(False, None, "secret_material")

        sanitized = text
        sanitized = self._EMAIL.sub("[REDACTED_EMAIL]", sanitized)
        public_urls = tuple(match.span() for match in re.finditer(r"https?://\S+", sanitized, re.I))

        def replace_phone(match: re.Match[str]) -> str:
            value = match.group(0)
            if any(start <= match.start() and match.end() <= end for start, end in public_urls):
                return value
            github_prefix = sanitized[max(0, match.start() - 256) : match.start()]
            github_suffix = sanitized[match.end() : match.end() + 1]
            if re.search(
                r'(?:^|["\'\s\[])gh:[A-Z0-9._-]+:'
                r"(?:issue:\d+:comment|pr:\d+:review|release):$",
                github_prefix,
                re.I,
            ) and (not github_suffix or github_suffix in {'"', "'", ",", "]", "}", "\n"}):
                return value
            digit_count = sum(character.isdigit() for character in value)
            if digit_count > 15:
                return value
            if value.isdigit():
                context = sanitized[max(0, match.start() - 40) : match.start()]
                surrounding = sanitized[
                    max(0, match.start() - 2) : min(len(sanitized), match.end() + 2)
                ]
                is_unix_timestamp = (
                    len(value) == 10
                    and 946_684_800 <= int(value) <= 4_102_444_800
                    and (
                        re.search(
                            r"(?:block\s+time|epoch|timestamp|unix)\s*[:=]?\s*$",
                            context,
                            re.I,
                        )
                        is not None
                        or any(character in surrounding for character in '{}[],=:"')
                    )
                )
                if is_unix_timestamp:
                    return value
            return "[REDACTED_PHONE]"

        sanitized = self._PHONE.sub(replace_phone, sanitized)
        sanitized = self._IPV4.sub("[REDACTED_IP]", sanitized)

        def replace_ipv6(match: re.Match[str]) -> str:
            try:
                parsed = ipaddress.ip_address(match.group(0))
            except ValueError:
                return match.group(0)
            return "[REDACTED_IP]" if parsed.version == 6 else match.group(0)

        sanitized = self._IPV6.sub(replace_ipv6, sanitized)
        sanitized = self._LOCAL_PATH.sub("[REDACTED_PATH]", sanitized)

        def replace_wallet(match: re.Match[str]) -> str:
            wallet = match.group(0)
            return wallet if wallet in self._public_wallet_allowlist else "[REDACTED_WALLET]"

        sanitized = self._WALLET.sub(replace_wallet, sanitized)
        return PrivacyResult(True, sanitized, None)

    def sanitize_payload(self, payload: Mapping[str, object]) -> dict[str, Any]:
        chat = payload.get("chat")
        if isinstance(chat, Mapping) and chat.get("type") in self._private_chat_types:
            raise PrivateChatRejected("private_chat")
        sanitized = self._sanitize_mapping(payload, parent_key=None)
        return sanitized

    def strip_internal_metadata(self, payload: Mapping[str, object]) -> dict[str, Any]:
        """Remove configured internal identifiers without changing public text."""

        return self._strip_internal_mapping(payload, parent_key=None)

    def _strip_internal_mapping(
        self, payload: Mapping[str, object], *, parent_key: str | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key in self._drop_numeric_id_keys or key in self._drop_path_keys:
                continue
            if key == "id" and parent_key in self._IDENTITY_CONTAINERS and isinstance(value, int):
                continue
            result[key] = self._strip_internal_value(value, parent_key=key)
        return result

    def _strip_internal_value(self, value: object, *, parent_key: str) -> Any:
        if isinstance(value, Mapping):
            return self._strip_internal_mapping(value, parent_key=parent_key)
        if isinstance(value, list):
            return [self._strip_internal_value(item, parent_key=parent_key) for item in value]
        return value

    def _sanitize_mapping(
        self, payload: Mapping[str, object], *, parent_key: str | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key in self._drop_numeric_id_keys or key in self._drop_path_keys:
                continue
            if key == "id" and parent_key in self._IDENTITY_CONTAINERS and isinstance(value, int):
                continue
            result[key] = self._sanitize_value(value, parent_key=key)
        return result

    def _sanitize_value(self, value: object, *, parent_key: str) -> Any:
        if isinstance(value, Mapping):
            return self._sanitize_mapping(value, parent_key=parent_key)
        if isinstance(value, list):
            return [self._sanitize_value(item, parent_key=parent_key) for item in value]
        if isinstance(value, str):
            inspected = self.inspect(value)
            if not inspected.accepted or inspected.sanitized_text is None:
                raise SensitiveContentRejected(inspected.reason_code or "unsafe_text")
            return inspected.sanitized_text
        return value

    def assert_public(self, text: str) -> None:
        inspected = self.inspect(text)
        if not inspected.accepted:
            raise PrivacyViolation(inspected.reason_code or "unsafe_text")
        if inspected.sanitized_text != text:
            raise PrivacyViolation("unredacted_personal_data")
