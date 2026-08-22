"""Scoped Ethereum Magicians collection through the public Discourse API."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Protocol, cast
from urllib.parse import quote, urljoin, urlparse

import httpx

from tawg_bot.ids import magicians_id
from tawg_bot.models import SourceCursors, SourceRecord, SourceType
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.storage import JsonlCollection
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class MagiciansSourceError(RuntimeError):
    """A safe Discourse collection failure."""


class MagiciansClient(Protocol):
    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> dict[str, Any]: ...


class MagiciansHttpClient:
    def __init__(self, *, base_url: str, client: httpx.AsyncClient) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Ethereum Magicians base URL must be HTTPS")
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def get_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(
                f"{self.base_url}{path}",
                params=cast(Any, params),
                headers={"User-Agent": "TAWGKnowledgeBot/0.1"},
            )
        except httpx.HTTPError:
            raise MagiciansSourceError("Ethereum Magicians HTTP request failed") from None
        if not response.is_success:
            raise MagiciansSourceError(
                f"Ethereum Magicians HTTP request returned status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise MagiciansSourceError("Ethereum Magicians response was not JSON") from None
        if not isinstance(payload, dict):
            raise MagiciansSourceError("Ethereum Magicians response was not an object")
        return payload


@dataclass(frozen=True, slots=True)
class TopicSeed:
    topic_id: int
    slug: str
    reason: str


@dataclass(frozen=True, slots=True)
class TopicCandidate:
    topic_id: int
    slug: str
    title: str
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class SeedResolution:
    seeds: tuple[TopicSeed, ...]
    candidates: tuple[TopicCandidate, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TopicBatch:
    records: tuple[SourceRecord, ...]
    cursor: str | None
    candidates: tuple[TopicCandidate, ...]


@dataclass(frozen=True, slots=True)
class MagiciansBatch:
    records: tuple[SourceRecord, ...]
    cursors: dict[str, str | int | None]
    candidates: tuple[TopicCandidate, ...]
    failed_topics: tuple[int, ...] = ()

    @property
    def successful(self) -> bool:
        return not self.failed_topics


class _CookedTextParser(HTMLParser):
    _BLOCKS = frozenset(
        {
            "blockquote",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "ol",
            "p",
            "pre",
            "table",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.links: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in self._BLOCKS:
            self.fragments.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if not self.ignored_depth and tag in self._BLOCKS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.fragments.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.fragments).splitlines()]
        return "\n".join(line for line in lines if line).strip()


class MagiciansSource:
    _ERC_TITLE = re.compile(r"^ERC-(?P<number>\d+)\b", re.I)

    def __init__(
        self,
        *,
        client: MagiciansClient,
        base_url: str,
        privacy: PrivacyFilter,
        now: Callable[[], datetime] | None = None,
        post_chunk_size: int = 20,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Ethereum Magicians base URL must be HTTPS")
        if not 1 <= post_chunk_size <= 20:
            raise ValueError("Discourse post chunk size must be between 1 and 20")
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.base_host = parsed.netloc.casefold()
        self.privacy = privacy
        self.now = now or (lambda: datetime.now(UTC))
        self.post_chunk_size = post_chunk_size

    async def resolve_seeds(
        self,
        *,
        erc_numbers: set[int],
        configured_urls: set[str],
        highlighted_urls: set[str],
        member_handles: set[str],
    ) -> SeedResolution:
        seeds: dict[int, TopicSeed] = {}
        candidates: dict[int, TopicCandidate] = {}
        failures: list[str] = []

        for url, reason in (
            *((value, "configured") for value in configured_urls),
            *((value, "telegram_highlight") for value in highlighted_urls),
        ):
            parsed = self._topic_from_url(url)
            if parsed is None:
                failures.append(f"topic-url:invalid:{self._safe_url_label(url)}")
                continue
            topic_id, slug = parsed
            seeds[topic_id] = TopicSeed(topic_id, slug, reason)

        for erc_number in sorted(erc_numbers):
            already_seeded = any(
                seed.slug.casefold().startswith(f"erc-{erc_number}-")
                for seed in seeds.values()
            )
            if already_seeded:
                continue
            payload = await self.client.get_json(
                "/search.json", {"q": f"ERC-{erc_number}", "expanded": "true"}
            )
            matches = [
                topic
                for topic in self._topics(payload)
                if self._title_erc_number(str(topic.get("title", ""))) == erc_number
            ]
            if len(matches) == 1:
                topic_id, slug, _ = self._topic_fields(matches[0])
                seeds[topic_id] = TopicSeed(topic_id, slug, "agent_erc")
                continue
            reason = "ambiguous" if matches else "not-found"
            failures.append(f"erc-{erc_number}:{reason}")
            for topic in matches:
                candidate = self._candidate_from_topic(topic, f"erc-{erc_number}:{reason}")
                candidates[candidate.topic_id] = candidate

        for handle in sorted(member_handles):
            safe_handle = handle.casefold().lstrip("@")
            if not re.fullmatch(r"[a-z0-9_.-]{1,64}", safe_handle):
                failures.append("member-handle:invalid")
                continue
            payload = await self.client.get_json(
                f"/topics/created-by/{quote(safe_handle, safe='')}.json"
            )
            for topic in self._topics(payload):
                candidate = self._candidate_from_topic(topic, "member_created")
                candidates[candidate.topic_id] = candidate

        for topic_id in seeds:
            candidates.pop(topic_id, None)
        return SeedResolution(
            seeds=tuple(seeds[topic_id] for topic_id in sorted(seeds)),
            candidates=tuple(candidates[topic_id] for topic_id in sorted(candidates)),
            failures=tuple(sorted(set(failures))),
        )

    async def sync_topic(self, seed: TopicSeed, cursor: str | int | None) -> TopicBatch:
        del cursor  # Every known post ID is revisited so edits cannot be missed.
        payload = await self.client.get_json(f"/t/{seed.topic_id}.json")
        topic_id = self._require_int(payload.get("id"), "topic ID")
        if topic_id != seed.topic_id:
            raise MagiciansSourceError("Ethereum Magicians returned an unexpected topic")
        slug = self._require_string(payload.get("slug"), "topic slug")
        title = self._require_string(payload.get("title"), "topic title")
        stream = self._require_mapping(payload.get("post_stream"))
        post_ids_raw = stream.get("stream")
        initial_posts_raw = stream.get("posts")
        if not isinstance(post_ids_raw, list) or not isinstance(initial_posts_raw, list):
            raise MagiciansSourceError("Ethereum Magicians post stream is invalid")
        post_ids = [self._require_int(value, "post ID") for value in post_ids_raw]
        posts = {
            self._require_int(post.get("id"), "post ID"): post
            for post in (self._require_mapping(value) for value in initial_posts_raw)
        }
        missing = [post_id for post_id in post_ids if post_id not in posts]
        for start in range(0, len(missing), self.post_chunk_size):
            chunk = missing[start : start + self.post_chunk_size]
            page = await self.client.get_json(
                f"/t/{seed.topic_id}/posts.json", {"post_ids[]": chunk}
            )
            page_stream = self._require_mapping(page.get("post_stream"))
            page_posts = page_stream.get("posts")
            if not isinstance(page_posts, list):
                raise MagiciansSourceError("Ethereum Magicians post page is invalid")
            for raw_post in page_posts:
                post = self._require_mapping(raw_post)
                posts[self._require_int(post.get("id"), "post ID")] = post
        absent = [post_id for post_id in post_ids if post_id not in posts]
        if absent:
            raise MagiciansSourceError("Ethereum Magicians omitted requested posts")

        records: list[SourceRecord] = []
        candidates: dict[int, TopicCandidate] = {}
        cursor_values: list[str] = []
        for post_id in post_ids:
            post = posts[post_id]
            record, links = self._record_from_post(topic_id, slug, title, post)
            if record is not None:
                records.append(record)
                cursor_values.append(record.updated_at.isoformat().replace("+00:00", "Z"))
            for link in links:
                parsed = self._topic_from_url(link)
                if parsed is None or parsed[0] == topic_id:
                    continue
                candidate_id, candidate_slug = parsed
                candidates[candidate_id] = TopicCandidate(
                    topic_id=candidate_id,
                    slug=candidate_slug,
                    title=candidate_slug.replace("-", " "),
                    url=self._canonical_topic_url(candidate_id, candidate_slug),
                    reason=f"linked_from:{topic_id}",
                )
        return TopicBatch(
            records=tuple(records),
            cursor=max(cursor_values) if cursor_values else None,
            candidates=tuple(candidates[key] for key in sorted(candidates)),
        )

    async def sync_all(
        self,
        seeds: Iterable[TopicSeed],
        cursors: SourceCursors,
        initial_candidates: Iterable[TopicCandidate] = (),
    ) -> MagiciansBatch:
        records: list[SourceRecord] = []
        next_cursors = dict(cursors.magicians)
        candidates = {candidate.topic_id: candidate for candidate in initial_candidates}
        failures: list[int] = []
        seed_ids = {seed.topic_id for seed in seeds}
        for seed in sorted(seeds, key=lambda item: item.topic_id):
            key = f"topic:{seed.topic_id}:updated_at"
            try:
                batch = await self.sync_topic(seed, cursors.magicians.get(key))
            except MagiciansSourceError:
                failures.append(seed.topic_id)
                continue
            records.extend(batch.records)
            if batch.cursor is not None:
                next_cursors[key] = batch.cursor
            for candidate in batch.candidates:
                if candidate.topic_id not in seed_ids:
                    candidates[candidate.topic_id] = candidate
        by_id = {record.record_id: record for record in records}
        return MagiciansBatch(
            records=tuple(by_id[key] for key in sorted(by_id)),
            cursors=next_cursors,
            candidates=tuple(candidates[key] for key in sorted(candidates)),
            failed_topics=tuple(failures),
        )

    def stage_batch(
        self,
        batch: MagiciansBatch,
        cursors: SourceCursors,
        uow: RepositoryUnitOfWork,
    ) -> None:
        monthly: dict[str, list[SourceRecord]] = {}
        for record in batch.records:
            path = f"data/magicians/{record.created_at:%Y/%m}/posts.jsonl"
            monthly.setdefault(path, []).append(record)
        for path, records in sorted(monthly.items()):
            collection = JsonlCollection(uow.root / path, SourceRecord)
            persisted = (
                collection.decode(collection.path.read_bytes())
                if collection.path.exists()
                else []
            )
            existing = {record.record_id: record for record in persisted}
            stable_records = [
                record.model_copy(update={"ingested_at": existing[record.record_id].ingested_at})
                if record.record_id in existing
                else record
                for record in records
            ]
            uow.stage_records(path, stable_records)
        cursors.magicians = batch.cursors
        uow.stage_json("data/state/source-cursors.json", cursors.model_dump(mode="json"))
        uow.stage_json(
            "data/state/magicians-candidates.json",
            [asdict(candidate) for candidate in batch.candidates],
        )

    def _record_from_post(
        self,
        topic_id: int,
        slug: str,
        title: str,
        post: dict[str, Any],
    ) -> tuple[SourceRecord | None, list[str]]:
        post_id = self._require_int(post.get("id"), "post ID")
        post_number = self._require_int(post.get("post_number"), "post number")
        username = self._require_string(post.get("username"), "post username")
        created_at = self._timestamp(post.get("created_at"))
        updated_at = self._timestamp(post.get("updated_at") or post.get("created_at"))
        cooked_value = post.get("cooked", "")
        if not isinstance(cooked_value, str):
            raise MagiciansSourceError("post body is invalid")
        parser = _CookedTextParser()
        parser.feed(cooked_value)
        text_result = self.privacy.inspect(parser.text())
        if not text_result.accepted or text_result.sanitized_text is None:
            return None, parser.links
        author_result = self.privacy.inspect(username)
        safe_author = (
            author_result.sanitized_text
            if author_result.accepted and author_result.sanitized_text
            else None
        )
        safe_title = self._safe_label(title, fallback=f"Topic {topic_id}")
        record = SourceRecord.from_text(
            record_id=magicians_id(topic_id, post_id),
            source_type=SourceType.MAGICIANS_POST,
            source_locator=f"{self._canonical_topic_url(topic_id, slug)}/{post_number}",
            author_person_id=safe_author.casefold() if safe_author else None,
            author_source_handle=safe_author,
            created_at=created_at,
            updated_at=max(created_at, updated_at),
            text_original=text_result.sanitized_text,
            ingested_at=self.now(),
            source_payload={
                "topic_id": topic_id,
                "post_id": post_id,
                "post_number": post_number,
                "topic_title": safe_title,
            },
        )
        return record, parser.links

    def _topic_from_url(self, value: str) -> tuple[int, str] | None:
        absolute = urljoin(f"{self.base_url}/", value)
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or parsed.netloc.casefold() != self.base_host:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "t":
            return None
        try:
            if parts[1].isdigit():
                return int(parts[1]), "topic"
            return int(parts[2]), parts[1]
        except ValueError:
            return None

    def _candidate_from_topic(self, topic: dict[str, Any], reason: str) -> TopicCandidate:
        topic_id, slug, title = self._topic_fields(topic)
        return TopicCandidate(
            topic_id=topic_id,
            slug=slug,
            title=self._safe_label(title, fallback=f"Topic {topic_id}"),
            url=self._canonical_topic_url(topic_id, slug),
            reason=reason,
        )

    def _canonical_topic_url(self, topic_id: int, slug: str) -> str:
        return f"{self.base_url}/t/{quote(slug, safe='-')}/{topic_id}"

    def _safe_label(self, value: str, *, fallback: str) -> str:
        inspected = self.privacy.inspect(value)
        if not inspected.accepted or not inspected.sanitized_text:
            return fallback
        return inspected.sanitized_text

    @classmethod
    def _title_erc_number(cls, title: str) -> int | None:
        match = cls._ERC_TITLE.match(title.strip())
        return int(match.group("number")) if match else None

    @staticmethod
    def _safe_url_label(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    @staticmethod
    def _topics(payload: dict[str, Any]) -> list[dict[str, Any]]:
        direct = payload.get("topics")
        if isinstance(direct, list):
            return [MagiciansSource._require_mapping(topic) for topic in direct]
        topic_list = payload.get("topic_list")
        if isinstance(topic_list, dict) and isinstance(topic_list.get("topics"), list):
            return [MagiciansSource._require_mapping(topic) for topic in topic_list["topics"]]
        raise MagiciansSourceError("Ethereum Magicians topic list is invalid")

    @staticmethod
    def _topic_fields(topic: dict[str, Any]) -> tuple[int, str, str]:
        return (
            MagiciansSource._require_int(topic.get("id"), "topic ID"),
            MagiciansSource._require_string(topic.get("slug"), "topic slug"),
            MagiciansSource._require_string(topic.get("title"), "topic title"),
        )

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise MagiciansSourceError("Ethereum Magicians timestamp is missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise MagiciansSourceError("Ethereum Magicians timestamp has no timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _require_mapping(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise MagiciansSourceError("Ethereum Magicians object is invalid")
        return value

    @staticmethod
    def _require_string(value: object, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise MagiciansSourceError(f"{label} is missing")
        return value

    @staticmethod
    def _require_int(value: object, label: str) -> int:
        if not isinstance(value, int) or value <= 0:
            raise MagiciansSourceError(f"{label} is missing")
        return value
