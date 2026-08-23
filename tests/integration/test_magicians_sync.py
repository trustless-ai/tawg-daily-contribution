import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tawg_bot.magicians_source import (
    MagiciansSource,
    MagiciansSourceError,
    TopicCandidate,
    TopicSeed,
)
from tawg_bot.models import SourceCursors
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.unit_of_work import RepositoryUnitOfWork

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)


class TopicClient:
    def __init__(self) -> None:
        self.topic = json.loads((ROOT / "tests/fixtures/magicians/topic-25098.json").read_text())
        self.posts = json.loads(
            (ROOT / "tests/fixtures/magicians/topic-25098-posts.json").read_text()
        )
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_json(self, path: str, params=None):
        query = params or {}
        self.calls.append((path, query))
        if path == "/t/25098.json":
            return self.topic
        if path == "/t/25098/posts.json":
            requested = set(query["post_ids[]"])
            posts = [post for post in self.posts["post_stream"]["posts"] if post["id"] in requested]
            return {"post_stream": {"posts": posts}}
        raise AssertionError(f"candidate topic was recursively fetched: {path}")


@pytest.mark.asyncio
async def test_topic_sync_fetches_every_stream_post_and_surfaces_links_as_candidates() -> None:
    client = TopicClient()
    source = MagiciansSource(
        client=client,
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
        now=lambda: NOW,
        post_chunk_size=2,
    )

    batch = await source.sync_topic(
        TopicSeed(25098, "erc-8004-trustless-agents", "configured"), cursor=None
    )

    assert [record.record_id for record in batch.records] == [
        "magicians:25098:post:501",
        "magicians:25098:post:502",
        "magicians:25098:post:503",
    ]
    assert batch.records[2].text_original == "Edited: on-chain reads remain important."
    assert "steal()" not in batch.records[0].text_original
    assert batch.records[0].source_locator.endswith("/25098/1")
    assert [candidate.topic_id for candidate in batch.candidates] == [26000]
    assert not any(path == "/t/26000.json" for path, _ in client.calls)
    post_calls = [query for path, query in client.calls if path.endswith("/posts.json")]
    assert post_calls == [{"post_ids[]": [502, 503]}]


@pytest.mark.asyncio
async def test_edited_post_keeps_stable_id_and_changes_content() -> None:
    client = TopicClient()
    source = MagiciansSource(
        client=client,
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
        now=lambda: NOW,
    )
    seed = TopicSeed(25098, "erc-8004-trustless-agents", "configured")
    first = await source.sync_topic(seed, cursor=None)
    client.posts["post_stream"]["posts"][1]["cooked"] = "<p>Edited again.</p>"
    client.posts["post_stream"]["posts"][1]["updated_at"] = "2026-08-23T00:30:00Z"

    second = await source.sync_topic(seed, cursor=first.cursor)

    before = next(record for record in first.records if record.record_id.endswith(":503"))
    after = next(record for record in second.records if record.record_id.endswith(":503"))
    assert before.record_id == after.record_id
    assert before.content_sha256 != after.content_sha256
    assert after.text_original == "Edited again."


@pytest.mark.asyncio
async def test_topic_sync_fails_safely_before_exceeding_its_post_budget() -> None:
    client = TopicClient()
    source = MagiciansSource(
        client=client,
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
        now=lambda: NOW,
        max_topic_posts=2,
    )

    with pytest.raises(MagiciansSourceError, match="post budget"):
        await source.sync_topic(
            TopicSeed(25098, "erc-8004-trustless-agents", "configured"), cursor=None
        )

    assert not any(path.endswith("/posts.json") for path, _ in client.calls)


@pytest.mark.asyncio
async def test_sync_batch_cannot_persist_external_posts_or_candidates(tmp_path: Path) -> None:
    seed = TopicSeed(25098, "erc-8004-trustless-agents", "configured")
    source = MagiciansSource(
        client=TopicClient(),
        base_url="https://ethereum-magicians.org",
        privacy=PrivacyFilter.from_yaml(ROOT / "config/privacy.yml"),
        now=lambda: NOW,
    )
    cursors = SourceCursors()
    candidate = TopicCandidate(
        27000,
        "community-agent-coordination",
        "Community agent coordination",
        "https://ethereum-magicians.org/t/community-agent-coordination/27000",
        "member_created",
    )
    promoted = TopicCandidate(
        25098,
        "erc-8004-trustless-agents",
        "ERC-8004 Trustless Agents",
        "https://ethereum-magicians.org/t/erc-8004-trustless-agents/25098",
        "member_created",
    )
    batch = await source.sync_all([seed], cursors, [candidate, promoted])
    first_uow = RepositoryUnitOfWork(tmp_path, operation_id="magicians-first")

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        source.stage_batch(batch, cursors, first_uow)

    assert not (tmp_path / "data/magicians").exists()
    assert not (tmp_path / "data/state/magicians-candidates.json").exists()
