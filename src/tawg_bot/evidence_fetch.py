"""Credential-free, policy-confined fetching for transient public evidence."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from tawg_bot.source_registry import RegisteredSource, SourceStatus


class EvidenceFetchRejected(RuntimeError):
    """A safe fetch failure exposing only a bounded machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HostResolver(Protocol):
    async def resolve(
        self, host: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]: ...


class SystemHostResolver:
    async def resolve(
        self, host: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records}
        except (OSError, ValueError):
            raise EvidenceFetchRejected("dns_policy") from None
        return tuple(sorted(addresses, key=lambda value: (value.version, value.packed)))


@dataclass(slots=True)
class FetchBudget:
    max_sources: int
    max_total_bytes: int
    deadline: datetime
    sources_consumed: int = 0
    bytes_consumed: int = 0

    def __post_init__(self) -> None:
        if self.max_sources <= 0 or self.max_total_bytes <= 0:
            raise ValueError("fetch budget limits must be positive")
        _require_utc(self.deadline, "fetch budget deadline")

    def reserve_source(self, now: datetime) -> None:
        _require_utc(now, "fetch time")
        if now >= self.deadline:
            raise EvidenceFetchRejected("operation_deadline")
        if self.sources_consumed >= self.max_sources:
            raise EvidenceFetchRejected("source_budget")
        self.sources_consumed += 1

    def assert_fits(self, size: int) -> None:
        if self.bytes_consumed + size > self.max_total_bytes:
            raise EvidenceFetchRejected("operation_size_policy")

    def consume(self, size: int) -> None:
        self.assert_fits(size)
        self.bytes_consumed += size


@dataclass(frozen=True, slots=True)
class FetchedEvidence:
    source_key: str
    canonical_url: str
    citation_url: str
    media_type: str
    observed_at: datetime
    version: str | None
    content_sha256: str
    byte_count: int
    text: str


class RestrictedEvidenceFetcher:
    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolver: HostResolver | None = None,
        max_redirects: int = 3,
        timeout_seconds: float = 20.0,
    ) -> None:
        if max_redirects < 0 or max_redirects > 8:
            raise ValueError("max_redirects must be between 0 and 8")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        self.client = client
        self.resolver = resolver or SystemHostResolver()
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds

    async def fetch(
        self,
        source: RegisteredSource,
        *,
        now: datetime,
        budget: FetchBudget | None = None,
    ) -> FetchedEvidence:
        _require_utc(now, "evidence observation time")
        if source.status is not SourceStatus.ACTIVE:
            raise EvidenceFetchRejected("source_status")
        if budget is not None:
            budget.reserve_source(now)
            remaining = (budget.deadline - now).total_seconds()
            timeout = min(self.timeout_seconds, remaining)
        else:
            timeout = self.timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                return await self._fetch(source, now=now, budget=budget)
        except TimeoutError:
            raise EvidenceFetchRejected("timeout") from None
        except EvidenceFetchRejected:
            raise
        except httpx.HTTPError:
            raise EvidenceFetchRejected("network") from None

    async def _fetch(
        self,
        source: RegisteredSource,
        *,
        now: datetime,
        budget: FetchBudget | None,
    ) -> FetchedEvidence:
        current_url = source.canonical_url
        redirects = 0
        while True:
            await self._assert_public_target(source, current_url)
            request = httpx.Request(
                "GET",
                current_url,
                headers={
                    "Accept": ", ".join(source.fetch_policy.mime_types),
                    "User-Agent": "TAWGKnowledgeBot/0.2",
                },
            )
            response = await self.client.send(request, stream=True)
            try:
                if response.status_code in self._REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if location is None or redirects >= self.max_redirects:
                        raise EvidenceFetchRejected("redirect_policy")
                    redirected = urljoin(current_url, location)
                    if not source.fetch_policy.accepts_url(redirected):
                        raise EvidenceFetchRejected("redirect_policy")
                    current_url = redirected
                    redirects += 1
                    continue
                if not response.is_success:
                    raise EvidenceFetchRejected("http_status")
                media_type = self._media_type(response)
                declared = self._declared_size(response)
                if declared is not None:
                    self._assert_size(source, declared, budget)
                payload = await self._bounded_body(response, source, budget)
                text = self._decode(payload)
                version = self._version(response)
                return FetchedEvidence(
                    source_key=source.source_key,
                    canonical_url=source.canonical_url,
                    citation_url=current_url,
                    media_type=media_type,
                    observed_at=now,
                    version=version,
                    content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    byte_count=len(payload),
                    text=text,
                )
            finally:
                await response.aclose()

    async def _assert_public_target(self, source: RegisteredSource, url: str) -> None:
        if not source.fetch_policy.accepts_url(url):
            raise EvidenceFetchRejected("url_policy")
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        addresses = await self.resolver.resolve(parsed.hostname, parsed.port or 443)
        if not addresses or any(
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            for address in addresses
        ):
            raise EvidenceFetchRejected("dns_policy")

    @staticmethod
    def _media_type(response: httpx.Response) -> str:
        value = str(response.headers.get("Content-Type", "")).split(";", 1)[0].casefold().strip()
        source = response.request
        allowed = source.headers.get("Accept", "")
        allowed_types = {item.strip().casefold() for item in allowed.split(",")}
        if not value or value not in allowed_types:
            raise EvidenceFetchRejected("mime_policy")
        return value

    @staticmethod
    def _declared_size(response: httpx.Response) -> int | None:
        value = response.headers.get("Content-Length")
        if value is None:
            return None
        try:
            size = int(value)
        except ValueError:
            raise EvidenceFetchRejected("size_policy") from None
        if size < 0:
            raise EvidenceFetchRejected("size_policy")
        return size

    @staticmethod
    def _assert_size(source: RegisteredSource, size: int, budget: FetchBudget | None) -> None:
        if size > source.fetch_policy.max_bytes:
            raise EvidenceFetchRejected("size_policy")
        if budget is not None:
            budget.assert_fits(size)

    async def _bounded_body(
        self,
        response: httpx.Response,
        source: RegisteredSource,
        budget: FetchBudget | None,
    ) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            self._assert_size(source, len(body), budget)
        if budget is not None:
            budget.consume(len(body))
        return bytes(body)

    @staticmethod
    def _decode(payload: bytes) -> str:
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise EvidenceFetchRejected("encoding_policy") from None
        normalized = unicodedata.normalize("NFC", decoded).replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()

    @staticmethod
    def _version(response: httpx.Response) -> str | None:
        etag = response.headers.get("ETag")
        value = etag.strip('"') if etag else response.headers.get("Last-Modified")
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized if 0 < len(normalized) <= 256 else None


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
