"""Credential-free, policy-confined fetching for transient public evidence."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from tawg_bot.source_registry import EvidenceKind, RegisteredSource, SourceStatus


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


@dataclass(slots=True)
class _RedirectBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise EvidenceFetchRejected("redirect_policy")
        self.remaining -= 1


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


class _DiscourseTextParser(HTMLParser):
    _BLOCKS = frozenset(
        {"blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.fragments.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.fragments).splitlines()]
        return "\n".join(line for line in lines if line).strip()


class RestrictedEvidenceFetcher:
    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
    _DISCOURSE_TOPIC = re.compile(r"^/t/[A-Za-z0-9._~-]+/(?P<topic_id>[1-9]\d*)/?$")

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
        topic_id = self._discourse_topic_id(source)
        if topic_id is not None:
            return await self._fetch_discourse_latest(
                source,
                topic_id=topic_id,
                now=now,
                budget=budget,
            )
        current_url, media_type, payload, version = await self._fetch_payload(
            source,
            source.canonical_url,
            budget=budget,
        )
        text = self._decode(payload)
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

    async def _fetch_payload(
        self,
        source: RegisteredSource,
        url: str,
        *,
        budget: FetchBudget | None,
        max_bytes: int | None = None,
        redirect_budget: _RedirectBudget | None = None,
    ) -> tuple[str, str, bytes, str | None]:
        if max_bytes is not None and max_bytes <= 0:
            raise EvidenceFetchRejected("size_policy")
        current_url = url
        redirects = redirect_budget or _RedirectBudget(self.max_redirects)
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
                    if location is None:
                        raise EvidenceFetchRejected("redirect_policy")
                    redirected = urljoin(current_url, location)
                    if not source.fetch_policy.accepts_url(redirected):
                        raise EvidenceFetchRejected("redirect_policy")
                    redirects.consume()
                    current_url = redirected
                    continue
                if not response.is_success:
                    raise EvidenceFetchRejected("http_status")
                media_type = self._media_type(response)
                declared = self._declared_size(response)
                if declared is not None:
                    self._assert_size(source, declared, budget, max_bytes=max_bytes)
                payload = await self._bounded_body(
                    response,
                    source,
                    budget,
                    max_bytes=max_bytes,
                )
                version = self._version(response)
                return current_url, media_type, payload, version
            finally:
                await response.aclose()

    async def _fetch_discourse_latest(
        self,
        source: RegisteredSource,
        *,
        topic_id: int,
        now: datetime,
        budget: FetchBudget | None,
    ) -> FetchedEvidence:
        base_url = source.canonical_url.rstrip("/")
        redirect_budget = _RedirectBudget(self.max_redirects)
        _, _, overview_payload, _ = await self._fetch_payload(
            source,
            f"{base_url}/1.json",
            budget=budget,
            redirect_budget=redirect_budget,
        )
        overview = self._discourse_document(overview_payload, topic_id=topic_id)
        highest = self._discourse_int(overview.get("highest_post_number"))
        if not 1 <= highest <= 1_000_000:
            raise EvidenceFetchRejected("content_policy")
        latest_payload = overview_payload
        if highest > 20:
            remaining_bytes = source.fetch_policy.max_bytes - len(overview_payload)
            _, _, latest_payload, _ = await self._fetch_payload(
                source,
                f"{base_url}/{highest}.json",
                budget=budget,
                max_bytes=remaining_bytes,
                redirect_budget=redirect_budget,
            )
        combined_size = len(overview_payload) + (
            0 if latest_payload is overview_payload else len(latest_payload)
        )
        if combined_size > source.fetch_policy.max_bytes:
            raise EvidenceFetchRejected("size_policy")
        latest = self._discourse_document(latest_payload, topic_id=topic_id)
        if self._discourse_int(latest.get("highest_post_number")) != highest:
            raise EvidenceFetchRejected("content_policy")
        text, version = self._render_discourse_latest(latest, highest=highest)
        return FetchedEvidence(
            source_key=source.source_key,
            canonical_url=source.canonical_url,
            citation_url=source.canonical_url,
            media_type="application/json",
            observed_at=now,
            version=version,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            byte_count=combined_size,
            text=text,
        )

    @classmethod
    def _discourse_topic_id(cls, source: RegisteredSource) -> int | None:
        parsed = urlsplit(source.canonical_url)
        if (
            source.kind is not EvidenceKind.DISCUSSION
            or parsed.hostname != "ethereum-magicians.org"
        ):
            return None
        match = cls._DISCOURSE_TOPIC.fullmatch(parsed.path)
        return int(match.group("topic_id")) if match is not None else None

    @staticmethod
    def _discourse_document(payload: bytes, *, topic_id: int) -> dict[str, object]:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EvidenceFetchRejected("content_policy") from None
        if (
            not isinstance(value, dict)
            or RestrictedEvidenceFetcher._discourse_int(value.get("id")) != topic_id
        ):
            raise EvidenceFetchRejected("content_policy")
        return value

    @staticmethod
    def _discourse_int(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvidenceFetchRejected("content_policy")
        return value

    @classmethod
    def _render_discourse_latest(
        cls,
        document: dict[str, object],
        *,
        highest: int,
    ) -> tuple[str, str]:
        title = document.get("title")
        last_posted_at = document.get("last_posted_at")
        stream = document.get("post_stream")
        posts = stream.get("posts") if isinstance(stream, dict) else None
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 512
            or not isinstance(last_posted_at, str)
            or not 1 <= len(last_posted_at) <= 64
            or not isinstance(posts, list)
            or not 1 <= len(posts) <= 20
        ):
            raise EvidenceFetchRejected("content_policy")
        rendered = [
            f"Topic: {title.strip()}",
            f"Latest visible post: {highest} at {last_posted_at}",
        ]
        seen_numbers: set[int] = set()
        for raw_post in posts:
            if not isinstance(raw_post, dict):
                raise EvidenceFetchRejected("content_policy")
            post_number = cls._discourse_int(raw_post.get("post_number"))
            username = raw_post.get("username")
            created_at = raw_post.get("created_at")
            cooked = raw_post.get("cooked")
            if (
                post_number in seen_numbers
                or not isinstance(username, str)
                or not 1 <= len(username) <= 128
                or not isinstance(created_at, str)
                or not 1 <= len(created_at) <= 64
                or not isinstance(cooked, str)
            ):
                raise EvidenceFetchRejected("content_policy")
            parser = _DiscourseTextParser()
            parser.feed(cooked)
            body = parser.text()
            if not body:
                continue
            seen_numbers.add(post_number)
            rendered.extend(
                (f"Post {post_number} by {username} at {created_at}", body)
            )
        if highest not in seen_numbers:
            raise EvidenceFetchRejected("content_policy")
        return "\n\n".join(rendered), last_posted_at

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
    def _assert_size(
        source: RegisteredSource,
        size: int,
        budget: FetchBudget | None,
        *,
        max_bytes: int | None = None,
    ) -> None:
        source_limit = source.fetch_policy.max_bytes
        if max_bytes is not None:
            source_limit = min(source_limit, max_bytes)
        if size > source_limit:
            raise EvidenceFetchRejected("size_policy")
        if budget is not None:
            budget.assert_fits(size)

    async def _bounded_body(
        self,
        response: httpx.Response,
        source: RegisteredSource,
        budget: FetchBudget | None,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            self._assert_size(source, len(body), budget, max_bytes=max_bytes)
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
