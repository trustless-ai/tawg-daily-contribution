from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tawg_bot.evidence_fetch import (
    EvidenceFetchRejected,
    FetchBudget,
    RestrictedEvidenceFetcher,
)
from tawg_bot.source_registry import RegisteredSource, SourceStatus

NOW = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


class Resolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        self.hosts: list[str] = []

    async def resolve(
        self, host: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        assert port == 443
        self.hosts.append(host)
        return self.addresses


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yield_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yield_count += 1
            yield chunk


def source(*, max_bytes: int = 100, mime_types: list[str] | None = None) -> RegisteredSource:
    return RegisteredSource.model_validate(
        {
            "source_key": "erc-8004-canonical",
            "topics": ["erc-8004"],
            "kind": "normative_spec",
            "authority": "canonical",
            "canonical_url": "https://evidence.example/evidence/current",
            "immutable_url": None,
            "fetch_policy": {
                "policy": "public-text",
                "allowed_hosts": ["evidence.example"],
                "allowed_path_prefixes": ["/evidence"],
                "max_bytes": max_bytes,
                "mime_types": mime_types or ["text/plain", "text/html"],
            },
            "last_observed": None,
            "status": "active",
        }
    )


def discourse_source(*, max_bytes: int = 1_000_000) -> RegisteredSource:
    return RegisteredSource.model_validate(
        {
            "source_key": "magicians-8183",
            "topics": ["erc-8183"],
            "kind": "discussion",
            "authority": "community",
            "canonical_url": (
                "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902"
            ),
            "immutable_url": None,
            "fetch_policy": {
                "policy": "public-text",
                "allowed_hosts": ["ethereum-magicians.org"],
                "allowed_path_prefixes": ["/t/erc-8183-agentic-commerce/27902"],
                "max_bytes": max_bytes,
                "mime_types": ["text/html", "text/plain", "application/json"],
            },
            "last_observed": None,
            "status": "active",
        }
    )


@pytest.mark.asyncio
async def test_fetch_returns_normalized_transient_evidence_without_ambient_credentials() -> None:
    seen_headers: httpx.Headers | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = request.headers
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8", "ETag": '"v1"'},
            content=b"hello\r\n",
        )

    resolver = Resolver("8.8.8.8")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Authorization": "Bearer secret"},
        cookies={"session": "secret"},
    ) as client:
        fetched = await RestrictedEvidenceFetcher(client=client, resolver=resolver).fetch(
            source(), now=NOW
        )

    assert fetched.text == "hello"
    assert fetched.content_sha256 == hashlib.sha256(b"hello").hexdigest()
    assert fetched.version == "v1"
    assert fetched.citation_url == "https://evidence.example/evidence/current"
    assert resolver.hosts == ["evidence.example"]
    assert seen_headers is not None
    assert "authorization" not in seen_headers
    assert "cookie" not in seen_headers


@pytest.mark.asyncio
async def test_discourse_discussion_fetches_the_latest_topic_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/1.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "id": 27902,
                    "title": "ERC-8183: Agentic Commerce",
                    "highest_post_number": 394,
                    "last_posted_at": "2026-08-24T15:58:38.080Z",
                    "post_stream": {
                        "posts": [
                            {
                                "post_number": 1,
                                "username": "author",
                                "created_at": "2026-03-04T21:49:13.338Z",
                                "cooked": "<p>Stale opening post.</p>",
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/394.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "id": 27902,
                    "title": "ERC-8183: Agentic Commerce",
                    "highest_post_number": 394,
                    "last_posted_at": "2026-08-24T15:58:38.080Z",
                    "post_stream": {
                        "posts": [
                            {
                                "post_number": 394,
                                "username": "latest-author",
                                "created_at": "2026-08-24T15:58:38.080Z",
                                "cooked": (
                                    "<p>Latest discussion focuses on evaluator "
                                    "responsiveness.</p>"
                                ),
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="Stale opening page only.",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetched = await RestrictedEvidenceFetcher(
            client=client,
            resolver=Resolver("8.8.8.8"),
        ).fetch(discourse_source(), now=NOW)

    assert calls == [
        "/t/erc-8183-agentic-commerce/27902/1.json",
        "/t/erc-8183-agentic-commerce/27902/394.json",
    ]
    assert "Post 394 by latest-author" in fetched.text
    assert "Latest discussion focuses on evaluator responsiveness." in fetched.text
    assert "Stale opening post" not in fetched.text
    assert fetched.citation_url == discourse_source().canonical_url
    assert fetched.version == "2026-08-24T15:58:38.080Z"


@pytest.mark.asyncio
async def test_discourse_fetch_stops_at_the_aggregate_source_byte_limit() -> None:
    overview_payload = json.dumps(
        {
            "id": 27902,
            "title": "ERC-8183",
            "highest_post_number": 394,
            "last_posted_at": "2026-08-24T15:58:38.080Z",
            "post_stream": {"posts": []},
        }
    ).encode()
    latest_payload = json.dumps(
        {
            "id": 27902,
            "title": "ERC-8183",
            "highest_post_number": 394,
            "last_posted_at": "2026-08-24T15:58:38.080Z",
            "post_stream": {
                "posts": [
                    {
                        "post_number": 394,
                        "username": "latest-author",
                        "created_at": "2026-08-24T15:58:38.080Z",
                        "cooked": f"<p>{'x' * 180}</p>",
                    }
                ]
            },
        }
    ).encode()
    max_bytes = max(len(overview_payload), len(latest_payload)) + 1
    remaining = max_bytes - len(overview_payload)
    assert 0 < remaining < len(latest_payload) < max_bytes
    first_chunk_size = max(1, remaining // 2)
    second_chunk_end = min(len(latest_payload), remaining + 1)
    stream = TrackingStream(
        [
            latest_payload[:first_chunk_size],
            latest_payload[first_chunk_size:second_chunk_end],
            latest_payload[second_chunk_end:],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=overview_payload,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="size_policy"):
            await RestrictedEvidenceFetcher(
                client=client,
                resolver=Resolver("8.8.8.8"),
            ).fetch(discourse_source(max_bytes=max_bytes), now=NOW)

    assert stream.yield_count == 2


@pytest.mark.asyncio
async def test_discourse_fetch_shares_one_redirect_budget_across_pages() -> None:
    calls: list[str] = []
    overview = {
        "id": 27902,
        "title": "ERC-8183",
        "highest_post_number": 394,
        "last_posted_at": "2026-08-24T15:58:38.080Z",
        "post_stream": {"posts": []},
    }
    latest = {
        **overview,
        "post_stream": {
            "posts": [
                {
                    "post_number": 394,
                    "username": "latest-author",
                    "created_at": "2026-08-24T15:58:38.080Z",
                    "cooked": "<p>Latest post.</p>",
                }
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/1.json"):
            return httpx.Response(302, headers={"Location": "overview.json"})
        if path.endswith("/overview.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=overview,
            )
        if path.endswith("/394.json"):
            return httpx.Response(302, headers={"Location": "latest.json"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=latest,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="redirect_policy"):
            await RestrictedEvidenceFetcher(
                client=client,
                resolver=Resolver("8.8.8.8"),
                max_redirects=1,
            ).fetch(discourse_source(), now=NOW)

    assert calls == [
        "/t/erc-8183-agentic-commerce/27902/1.json",
        "/t/erc-8183-agentic-commerce/27902/overview.json",
        "/t/erc-8183-agentic-commerce/27902/394.json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.1.1", "224.0.0.1", "192.0.2.1", "::1"],
)
async def test_fetch_rejects_non_global_dns_before_request(address: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="no")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="dns_policy"):
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver(address)).fetch(
                source(), now=NOW
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_fetch_does_not_follow_redirect_outside_source_policy() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="redirect_policy"):
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8")).fetch(
                source(), now=NOW
            )

    assert calls == ["https://evidence.example/evidence/current"]


@pytest.mark.asyncio
async def test_fetch_revalidates_an_allowed_redirect_before_following() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/evidence/current":
            return httpx.Response(302, headers={"Location": "/evidence/version-2"})
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="version two")

    resolver = Resolver("8.8.8.8")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetched = await RestrictedEvidenceFetcher(client=client, resolver=resolver).fetch(
            source(), now=NOW
        )

    assert calls == [
        "https://evidence.example/evidence/current",
        "https://evidence.example/evidence/version-2",
    ]
    assert resolver.hosts == ["evidence.example", "evidence.example"]
    assert fetched.citation_url == "https://evidence.example/evidence/version-2"


@pytest.mark.asyncio
async def test_fetch_bounds_redirect_count() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": f"/evidence/redirect-{calls}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="redirect_policy"):
            await RestrictedEvidenceFetcher(
                client=client, resolver=Resolver("8.8.8.8"), max_redirects=3
            ).fetch(source(), now=NOW)

    assert calls == 4


@pytest.mark.asyncio
async def test_fetch_rejects_non_allowlisted_mime_without_exposing_body() -> None:
    canary = "PRIVATE-EVIDENCE-CANARY"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            text=canary,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="mime_policy") as failure:
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8")).fetch(
                source(), now=NOW
            )

    assert canary not in str(failure.value)
    assert "evidence.example" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"Content-Type": "text/plain", "Content-Length": "6"}, b"123456"),
        ({"Content-Type": "text/plain"}, b"123456"),
    ],
)
async def test_fetch_enforces_declared_and_streamed_response_size(
    headers: dict[str, str], content: bytes
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="size_policy"):
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8")).fetch(
                source(max_bytes=5), now=NOW
            )


@pytest.mark.asyncio
async def test_fetch_timeout_uses_a_safe_error_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="late")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="timeout") as failure:
            await RestrictedEvidenceFetcher(
                client=client,
                resolver=Resolver("8.8.8.8"),
                timeout_seconds=0.001,
            ).fetch(source(), now=NOW)

    assert str(failure.value) == "timeout"


@pytest.mark.asyncio
async def test_fetch_budget_rejects_an_extra_source_before_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="ok")

    budget = FetchBudget(
        max_sources=1,
        max_total_bytes=10,
        deadline=NOW + timedelta(minutes=1),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8"))
        await fetcher.fetch(source(), now=NOW, budget=budget)
        with pytest.raises(EvidenceFetchRejected, match="source_budget"):
            await fetcher.fetch(source(), now=NOW, budget=budget)

    assert calls == 1


@pytest.mark.asyncio
async def test_fetch_budget_enforces_total_bytes_across_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="abc")

    budget = FetchBudget(
        max_sources=2,
        max_total_bytes=5,
        deadline=NOW + timedelta(minutes=1),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8"))
        await fetcher.fetch(source(), now=NOW, budget=budget)
        with pytest.raises(EvidenceFetchRejected, match="operation_size_policy"):
            await fetcher.fetch(source(), now=NOW, budget=budget)

    assert budget.bytes_consumed == 3


@pytest.mark.asyncio
async def test_fetch_rejects_expired_operation_budget_without_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="no")

    budget = FetchBudget(
        max_sources=1,
        max_total_bytes=10,
        deadline=NOW,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="operation_deadline"):
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8")).fetch(
                source(), now=NOW, budget=budget
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_fetch_rejects_candidate_source_before_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="no")

    candidate = source().model_copy(update={"status": SourceStatus.CANDIDATE})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceFetchRejected, match="source_status"):
            await RestrictedEvidenceFetcher(client=client, resolver=Resolver("8.8.8.8")).fetch(
                candidate, now=NOW
            )

    assert calls == 0
