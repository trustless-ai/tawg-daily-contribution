from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tawg_bot.magicians_source import (
    MagiciansHttpClient,
    MagiciansSource,
    MagiciansSourceError,
)
from tawg_bot.privacy import PrivacyFilter

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_http_client_retries_one_rate_limited_request() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"topics": []}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = MagiciansHttpClient(
            base_url="https://ethereum-magicians.org", client=client
        )
        payload = await source.get_json("/search.json", {"q": "ERC-8004"})

    assert payload == {"topics": []}
    assert attempts == 2


@pytest.mark.asyncio
async def test_http_client_respects_retry_after_http_date() -> None:
    attempts = 0
    delays: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Sun, 23 Aug 2026 01:01:00 GMT"},
                request=request,
            )
        return httpx.Response(200, json={"topics": []}, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = MagiciansHttpClient(
            base_url="https://ethereum-magicians.org",
            client=client,
            now=lambda: NOW,
            sleep=sleep,
        )
        await source.get_json("/search.json", {"q": "ERC-8004"})

    assert delays == [timedelta(minutes=1).total_seconds()]


@pytest.mark.asyncio
async def test_http_client_rejects_non_finite_retry_after_values() -> None:
    async with httpx.AsyncClient() as client:
        source = MagiciansHttpClient(
            base_url="https://ethereum-magicians.org",
            client=client,
            now=lambda: NOW,
        )

        assert source._retry_delay("inf", 0) == 1.0
        assert source._retry_delay("NaN", 1) == 2.0
        assert source._retry_delay("1e9", 2) == 4.0
        with pytest.raises(MagiciansSourceError, match="retry delay"):
            source._retry_delay("999999999", 0)
        with pytest.raises(MagiciansSourceError, match="retry delay"):
            source._retry_delay("Sun, 23 Aug 2026 02:00:00 GMT", 0)


class SearchClient:
    def __init__(self) -> None:
        import json

        self.search = json.loads(
            (ROOT / "tests/fixtures/magicians/search.json").read_text()
        )
        self.members = json.loads(
            (ROOT / "tests/fixtures/magicians/member-topics.json").read_text()
        )

    async def get_json(self, path: str, params=None):
        if path == "/search.json":
            erc = str(params["q"]).casefold()
            return {
                "topics": [
                    topic for topic in self.search["topics"] if erc in topic["title"].casefold()
                ],
                "posts": [],
            }
        if path == "/topics/created-by/alice.json":
            return self.members
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_seed_resolution_accepts_exact_urls_and_refuses_ambiguous_searches() -> None:
    source = MagiciansSource(
        client=SearchClient(),
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
        now=lambda: NOW,
    )

    resolution = await source.resolve_seeds(
        erc_numbers={8004, 8126},
        configured_urls={
            "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902"
        },
        highlighted_urls=set(),
        member_handles={"alice"},
    )

    assert {seed.topic_id for seed in resolution.seeds} == {25098, 27902}
    assert "erc-8126:ambiguous" in resolution.failures
    candidate_ids = {candidate.topic_id for candidate in resolution.candidates}
    assert {26010, 26011, 27000}.issubset(candidate_ids)
