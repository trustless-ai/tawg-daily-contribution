from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.magicians_source import MagiciansSource
from tawg_bot.privacy import PrivacyFilter

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


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
