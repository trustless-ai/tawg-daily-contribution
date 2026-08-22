"""Production composition for the scheduled repository-backed bot."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from tawg_bot.aliases import AliasRegistry
from tawg_bot.bot_router import BotReplyService, PreparedReply, ReplyRejected
from tawg_bot.claude_cli import ClaudeCli
from tawg_bot.daily import DailyReadiness, DailyService, DailyWindow, PreparedDaily
from tawg_bot.delivery import (
    DeliveryAmbiguous,
    DeliveryCheckpoint,
    DeliveryFailed,
    DeliveryService,
)
from tawg_bot.github_source import GitHubBatch, GitHubHttpClient, GitHubSource
from tawg_bot.knowledge_refresh import KnowledgeRefresh
from tawg_bot.magicians_source import (
    MagiciansHttpClient,
    MagiciansSource,
    TopicCandidate,
)
from tawg_bot.models import JobStatus, PendingBotJob, SourceCursors, SourceRecord
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.scheduler import Scheduler
from tawg_bot.storage import JsonlCollection
from tawg_bot.telegram_api import TelegramApi
from tawg_bot.telegram_intake import TelegramIntake
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import VaultLinter


class RuntimeFailure(RuntimeError):
    """A safe production-composition failure."""


class ScriptCheckpoint(DeliveryCheckpoint):
    def __init__(self, script: Path) -> None:
        self.script = script.resolve()

    async def publish(self, operation_id: str, root: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(self.script),
            operation_id,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        del stdout, stderr
        if process.returncode != 0:
            raise RuntimeFailure("repository checkpoint failed")


class ProductionRuntime:
    def __init__(self, root: Path, *, checkpoint: DeliveryCheckpoint) -> None:
        self.root = root.resolve()
        self.checkpoint = checkpoint

    @classmethod
    def from_environment(cls, root: Path) -> ProductionRuntime:
        return cls(root, checkpoint=ScriptCheckpoint(root / "scripts/commit_operation.sh"))

    async def tick(self, now: datetime, *, observe_only: bool) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(
                self.root, client=client, checkpoint=self.checkpoint, now=now
            )
            result = await Scheduler(self.root, pipeline=pipeline).tick(
                now, observe_only=observe_only
            )
        await self.checkpoint.publish(
            f"layer-success:{result.layer.name.casefold()}:{int(now.timestamp())}",
            self.root,
        )

    async def backfill(self, source: str) -> None:
        now = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(
                self.root, client=client, checkpoint=self.checkpoint, now=now
            )
            if source == "github":
                await pipeline.github_sync(now)
            elif source == "magicians":
                await pipeline.magicians_sync(now)
            else:
                raise ValueError("unsupported backfill source")
            await pipeline.publish_sources()
            await pipeline.publish_repository()

    async def daily_dry_run(self, window_end: datetime) -> None:
        _require_utc(window_end, "Daily dry-run end")
        now = datetime.now(UTC)
        if now < window_end:
            raise RuntimeFailure("Daily dry-run window cannot end in the future")
        async with httpx.AsyncClient(timeout=30) as client:
            pipeline = _LivePipeline(
                self.root, client=client, checkpoint=self.checkpoint, now=now
            )
            window = DailyWindow(
                start=window_end - timedelta(days=1),
                end=window_end,
                window_id=f"daily:{window_end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            )
            await pipeline.prepare_daily(window, dry_run=True)
            await pipeline.publish_repository()


class _LivePipeline:
    def __init__(
        self,
        root: Path,
        *,
        client: httpx.AsyncClient,
        checkpoint: DeliveryCheckpoint,
        now: datetime,
    ) -> None:
        self.root = root
        self.client = client
        self.checkpoint = checkpoint
        self.now = now
        self.ai = ClaudeCli(root=root)
        self.cursors = self._load_cursors()
        self.source_uow = RepositoryUnitOfWork(
            root, operation_id=f"sources:{int(now.timestamp())}"
        )
        self.telegram_synced_at: datetime | None = None
        self.github_synced_at: datetime | None = None
        self.magicians_synced_at: datetime | None = None
        self.knowledge_refreshed_at: datetime | None = None
        self.prepared_daily: PreparedDaily | None = None
        self.prepared_replies: list[PreparedReply] = []

    async def telegram_intake(self, now: datetime) -> None:
        api = TelegramApi.from_env(client=self.client)
        intake = TelegramIntake.from_env(root=self.root, api=api)
        await intake.collect(now)
        self.cursors = self._load_cursors()
        self.telegram_synced_at = now

    async def github_sync(self, now: datetime) -> None:
        source = GitHubSource.for_repository(
            root=self.root,
            client=GitHubHttpClient.from_env(client=self.client),
            now=lambda: now,
        )
        batch = await source.sync_all(self.cursors)
        if not batch.successful:
            raise RuntimeFailure("required GitHub repository sync failed")
        self._stage_github(batch)
        self.cursors.github = batch.cursors
        self.github_synced_at = now

    async def magicians_sync(self, now: datetime) -> None:
        config = self._source_config()
        magicians = config.get("magicians")
        if not isinstance(magicians, dict):
            raise RuntimeFailure("invalid Magicians source configuration")
        base_url = str(magicians.get("base_url", ""))
        source = MagiciansSource(
            client=MagiciansHttpClient(base_url=base_url, client=self.client),
            base_url=base_url,
            privacy=PrivacyFilter.from_yaml(self.root / "config/privacy.yml"),
            now=lambda: now,
        )
        configured = self._string_set(magicians.get("required_topic_urls", []))
        highlighted = self._string_set(magicians.get("highlighted_topic_urls", []))
        aliases = AliasRegistry.from_yaml(self.root / "knowledge/meta/aliases.yml")
        handles = {
            handle
            for identity in aliases.people.values()
            for handle in identity.get("handles", {}).get("magicians", [])
            if isinstance(handle, str)
        }
        ercs = {8004, 8183}
        for record in self._all_source_records():
            ercs.update(
                int(value)
                for value in re.findall(r"\bERC-(\d{3,5})\b", record.text_original)
            )
        resolution = await source.resolve_seeds(
            erc_numbers=ercs,
            configured_urls=configured,
            highlighted_urls=highlighted,
            member_handles=handles,
        )
        configured_ids = {self._topic_id(url) for url in configured}
        resolved_ids = {seed.topic_id for seed in resolution.seeds}
        if None in configured_ids or not configured_ids.issubset(resolved_ids):
            raise RuntimeFailure("required Ethereum Magicians topic resolution failed")
        batch = await source.sync_all(
            resolution.seeds,
            self.cursors,
            [*resolution.candidates, *self._load_candidates()],
        )
        if not batch.successful:
            raise RuntimeFailure("required Ethereum Magicians topic sync failed")
        self._stage_records("magicians", batch.records)
        self.cursors.magicians = batch.cursors
        self.source_uow.stage_json(
            "data/state/magicians-candidates.json",
            [asdict(candidate) for candidate in batch.candidates],
        )
        self.magicians_synced_at = now

    async def publish_sources(self) -> None:
        self.source_uow.stage_json(
            "data/state/source-cursors.json", self.cursors.model_dump(mode="json")
        )
        self.source_uow.publish()
        await self.checkpoint.publish(f"sources:{int(self.now.timestamp())}", self.root)

    async def knowledge_refresh(self, cutoff: datetime) -> None:
        await KnowledgeRefresh(self.root, ai=self.ai).run(
            cutoff=cutoff,
            operation_id=f"knowledge-refresh-{int(cutoff.timestamp())}",
        )
        self.knowledge_refreshed_at = self.now

    async def validate(self) -> None:
        report = VaultLinter(self.root).lint(now=self.now)
        if report.error_count:
            raise RuntimeFailure("vault validation failed")

    async def daily_prepare(self, window_id: str) -> None:
        window = DailyWindow.for_due_run(self.now)
        if window.window_id != window_id:
            raise RuntimeFailure("scheduler supplied an inconsistent Daily window")
        await self.prepare_daily(window, dry_run=False)

    async def prepare_daily(self, window: DailyWindow, *, dry_run: bool) -> None:
        ready_at = self.now
        readiness = DailyReadiness(
            telegram_synced_at=self.telegram_synced_at or ready_at,
            github_synced_at=self.github_synced_at or ready_at,
            magicians_synced_at=self.magicians_synced_at or ready_at,
            knowledge_refreshed_at=self.knowledge_refreshed_at or ready_at,
        )
        prepared = await DailyService(self.root, ai=self.ai).prepare(
            window, readiness=readiness
        )
        if prepared is None:
            self.prepared_daily = None
            return
        self.prepared_daily = prepared
        artifact = {
            "schema": "tawg.prepared-daily.v1",
            "dry_run": dry_run,
            "window_id": prepared.window_id,
            "telegram_text": prepared.telegram_text,
            "citations": list(prepared.citations),
            "prepared_at": self.now.isoformat().replace("+00:00", "Z"),
        }
        uow = RepositoryUnitOfWork(
            self.root, operation_id=f"daily-prepared:{int(window.end.timestamp())}"
        )
        uow.stage_json("data/state/prepared-daily.json", artifact)
        uow.publish()

    async def publish_repository(self) -> None:
        await self._prepare_pending_replies()
        await self.checkpoint.publish(f"prepared:{int(self.now.timestamp())}", self.root)

    async def telegram_delivery(self) -> None:
        api = TelegramApi.from_env(client=self.client)
        chat_id = self._chat_id()
        delivery = DeliveryService(
            self.root,
            api=api,
            chat_id=chat_id,
            checkpoint=self.checkpoint,
        )
        for reply in self.prepared_replies:
            try:
                await delivery.deliver(
                    job_id=reply.job_id,
                    text=reply.reply_text,
                    reply_to_message_id=reply.reply_to_message_id,
                    now=self.now,
                )
            except (DeliveryAmbiguous, DeliveryFailed):
                continue
        if self.prepared_daily is not None:
            await delivery.deliver(
                job_id=self.prepared_daily.window_id,
                text=self.prepared_daily.telegram_text,
                reply_to_message_id=None,
                now=self.now,
            )

    async def _prepare_pending_replies(self) -> None:
        jobs = self._load_jobs()
        actionable = [
            job for job in jobs if job.status in {JobStatus.PENDING, JobStatus.READY}
        ]
        if not actionable:
            self.prepared_replies = []
            return
        username = os.environ.get("TAWG_TELEGRAM_BOT_USERNAME")
        if not username:
            raise RuntimeFailure("TAWG_TELEGRAM_BOT_USERNAME is not configured")
        service = BotReplyService(self.root, ai=self.ai, bot_username=username)
        self.prepared_replies = []
        for job in actionable:
            try:
                self.prepared_replies.append(
                    await service.prepare(job.job_id, now=self.now)
                )
            except ReplyRejected:
                continue

    def _stage_github(self, batch: GitHubBatch) -> None:
        self._stage_records("github", batch.records)

    def _stage_records(self, source: str, records: tuple[SourceRecord, ...]) -> None:
        monthly: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in records:
            name = "records.jsonl" if source == "github" else "posts.jsonl"
            if source == "github":
                repository = record.record_id.split(":", 2)[1]
                path = f"data/github/{repository}/{record.created_at:%Y/%m}/{name}"
            else:
                path = f"data/magicians/{record.created_at:%Y/%m}/{name}"
            monthly[path].append(record)
        for path, incoming in sorted(monthly.items()):
            collection = JsonlCollection(self.root / path, SourceRecord)
            existing = (
                {item.record_id: item for item in collection.decode(collection.path.read_bytes())}
                if collection.path.exists()
                else {}
            )
            stable = [
                item.model_copy(update={"ingested_at": existing[item.record_id].ingested_at})
                if item.record_id in existing
                else item
                for item in incoming
            ]
            self.source_uow.stage_records(path, stable)

    def _load_cursors(self) -> SourceCursors:
        return SourceCursors.model_validate_json(
            (self.root / "data/state/source-cursors.json").read_text(encoding="utf-8")
        )

    def _all_source_records(self) -> tuple[SourceRecord, ...]:
        from tawg_bot.query import SourceQuery

        return SourceQuery(self.root).records()

    def _source_config(self) -> dict[str, Any]:
        raw = yaml.safe_load((self.root / "config/sources.yml").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != "tawg.sources.v1":
            raise RuntimeFailure("invalid source configuration")
        return raw

    def _load_candidates(self) -> list[TopicCandidate]:
        path = self.root / "data/state/magicians-candidates.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(raw, list):
            raise RuntimeFailure("invalid Magicians candidate state")
        return [TopicCandidate(**item) for item in raw if isinstance(item, dict)]

    def _load_jobs(self) -> list[PendingBotJob]:
        raw = json.loads(
            (self.root / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, list):
            raise RuntimeFailure("invalid pending bot job state")
        return [PendingBotJob.model_validate(item) for item in raw]

    @staticmethod
    def _string_set(value: object) -> set[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RuntimeFailure("source URL configuration must be a string list")
        return set(value)

    @staticmethod
    def _topic_id(url: str) -> int | None:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if len(parts) < 3 or parts[0] != "t":
            return None
        try:
            return int(parts[1] if parts[1].isdigit() else parts[2])
        except ValueError:
            return None

    @staticmethod
    def _chat_id() -> int:
        raw = os.environ.get("TAWG_TELEGRAM_CHAT_ID")
        if not raw:
            raise RuntimeFailure("TAWG_TELEGRAM_CHAT_ID is not configured")
        try:
            return int(raw)
        except ValueError:
            raise RuntimeFailure("TAWG_TELEGRAM_CHAT_ID must be an integer") from None


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
