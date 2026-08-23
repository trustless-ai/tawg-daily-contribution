"""Durable L1 ingestion of all supported messages from one Telegram group."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from tawg_bot.aliases import AliasRegistry
from tawg_bot.ids import telegram_id
from tawg_bot.models import (
    AttachmentMetadata,
    PendingBotJob,
    Relation,
    SourceCursors,
    SourceRecord,
    SourceType,
)
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.storage import partition_stable_records
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class TelegramUpdateApi(Protocol):
    async def get_all_updates(self, offset: int, limit: int = 100) -> list[dict[str, Any]]: ...


UnitOfWorkFactory = Callable[[Path, str], RepositoryUnitOfWork]


@dataclass(frozen=True, slots=True)
class IntakeResult:
    received: int
    persisted: int
    filtered: int
    rejected: int
    jobs_created: int
    next_offset: int
    changed_paths: tuple[str, ...]


def _default_uow_factory(root: Path, operation_id: str) -> RepositoryUnitOfWork:
    return RepositoryUnitOfWork(root, operation_id=operation_id)


class TelegramIntake:
    def __init__(
        self,
        *,
        root: Path,
        api: TelegramUpdateApi,
        chat_id: int,
        group_slug: str,
        bot_username: str,
        uow_factory: UnitOfWorkFactory = _default_uow_factory,
    ) -> None:
        self.root = root
        self.api = api
        self.chat_id = chat_id
        self.group_slug = group_slug
        self.bot_username = bot_username.casefold().lstrip("@")
        self.uow_factory = uow_factory
        self.privacy = PrivacyFilter.from_yaml(root / "config/privacy.yml")
        self.aliases = AliasRegistry.from_yaml(root / "knowledge/meta/aliases.yml")

    @classmethod
    def from_env(
        cls,
        *,
        root: Path,
        api: TelegramUpdateApi,
        group_slug: str = "tawg",
        uow_factory: UnitOfWorkFactory = _default_uow_factory,
    ) -> TelegramIntake:
        raw_chat_id = os.environ.get("TAWG_TELEGRAM_CHAT_ID")
        bot_username = os.environ.get("TAWG_TELEGRAM_BOT_USERNAME")
        if not raw_chat_id or not bot_username:
            raise ValueError("Telegram group identity environment is not configured")
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            raise ValueError("TAWG_TELEGRAM_CHAT_ID must be an integer") from None
        return cls(
            root=root,
            api=api,
            chat_id=chat_id,
            group_slug=group_slug,
            bot_username=bot_username,
            uow_factory=uow_factory,
        )

    async def collect(self, now: datetime) -> IntakeResult:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("collection time must use UTC")
        cursors = SourceCursors.model_validate_json(
            (self.root / "data/state/source-cursors.json").read_text(encoding="utf-8")
        )
        updates = await self.api.get_all_updates(cursors.telegram_offset)
        if not updates:
            return IntakeResult(0, 0, 0, 0, 0, cursors.telegram_offset, ())

        next_offset = max(self._update_id(update) for update in updates) + 1
        records_by_id: dict[str, SourceRecord] = {}
        jobs_by_id = self._load_jobs()
        initial_job_ids = set(jobs_by_id)
        filtered = 0
        rejected = 0
        for update in updates:
            extracted = self._extract_message(update)
            if extracted is None:
                continue
            message, edited = extracted
            chat = message.get("chat")
            if not isinstance(chat, dict) or chat.get("id") != self.chat_id:
                filtered += 1
                continue
            record = self._record_from_message(message, edited=edited, ingested_at=now)
            if record is None:
                rejected += 1
                continue
            records_by_id[record.record_id] = record
            if self._triggers_reply(message):
                job_id = f"reply:{record.record_id}"
                existing = jobs_by_id.get(job_id)
                jobs_by_id[job_id] = existing or PendingBotJob(
                    job_id=job_id,
                    trigger_record_id=record.record_id,
                    reply_to_message_id=int(message["message_id"]),
                    created_at=now,
                    updated_at=now,
                )

        monthly: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in records_by_id.values():
            monthly[f"data/telegram/{record.created_at:%Y/%m}/messages.jsonl"].append(record)

        cursors.telegram_offset = next_offset
        uow = self.uow_factory(self.root, f"telegram-{uuid4()}")
        uow.register_external_evidence(())
        for path, month_records in sorted(monthly.items()):
            partitions = partition_stable_records(
                self.root,
                path,
                month_records,
                search_relative_root="data/telegram",
            )
            for target, stable_records in sorted(partitions.items()):
                uow.stage_records(target, stable_records)
        uow.stage_json(
            "data/state/pending-bot-jobs.json",
            [jobs_by_id[job_id].model_dump(mode="json") for job_id in sorted(jobs_by_id)],
        )
        uow.stage_json("data/state/source-cursors.json", cursors.model_dump(mode="json"))
        uow.stage_bytes("knowledge/meta/aliases.yml", self.aliases.to_yaml_bytes())
        changed_paths = uow.publish().changed_paths
        return IntakeResult(
            received=len(updates),
            persisted=len(records_by_id),
            filtered=filtered,
            rejected=rejected,
            jobs_created=len(set(jobs_by_id) - initial_job_ids),
            next_offset=next_offset,
            changed_paths=changed_paths,
        )

    def _record_from_message(
        self, message: dict[str, Any], *, edited: bool, ingested_at: datetime
    ) -> SourceRecord | None:
        message_id = message.get("message_id")
        created_timestamp = message.get("date")
        if not isinstance(message_id, int) or not isinstance(created_timestamp, int):
            return None
        text = message.get("text", message.get("caption", ""))
        if not isinstance(text, str):
            return None
        inspected = self.privacy.inspect(text)
        if not inspected.accepted or inspected.sanitized_text is None:
            return None
        author = message.get("from")
        author_mapping = author if isinstance(author, dict) else {}
        username = author_mapping.get("username")
        public_username = username if isinstance(username, str) else None
        display_name = self._display_name(author_mapping)
        person_id = self.aliases.resolve_telegram_live(
            public_username=public_username,
            display_name=display_name,
        )
        created_at = datetime.fromtimestamp(created_timestamp, tz=UTC)
        edit_timestamp = message.get("edit_date")
        updated_at = (
            datetime.fromtimestamp(edit_timestamp, tz=UTC)
            if isinstance(edit_timestamp, int)
            else created_at
        )
        record_id = telegram_id(self.group_slug, message_id)
        relations: list[Relation] = []
        reply = message.get("reply_to_message")
        if isinstance(reply, dict) and isinstance(reply.get("message_id"), int):
            relations.append(
                Relation(
                    relation_type="reply_to",
                    target_record_id=telegram_id(self.group_slug, reply["message_id"]),
                )
            )
        month_path = f"data/telegram/{created_at:%Y/%m}/messages.jsonl"
        return SourceRecord.from_text(
            record_id=record_id,
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator=f"repo:{month_path}#{record_id}",
            author_person_id=person_id,
            author_source_handle=f"@{public_username}" if public_username else display_name,
            created_at=created_at,
            updated_at=updated_at,
            text_original=inspected.sanitized_text,
            relations=relations,
            attachment_metadata=self._attachment_metadata(message, bool(text)),
            ingested_at=ingested_at,
            source_payload={"message_kind": "group_message", "edited": edited},
        )

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        raw = json.loads(
            (self.root / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
        )
        jobs = [PendingBotJob.model_validate(item) for item in raw]
        return {job.job_id: job for job in jobs}

    def _triggers_reply(self, message: dict[str, Any]) -> bool:
        text = message.get("text", message.get("caption", ""))
        entities = message.get("entities", message.get("caption_entities", []))
        if not isinstance(text, str) or not isinstance(entities, list):
            return False
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            entity_type = entity.get("type")
            offset = entity.get("offset")
            length = entity.get("length")
            if not isinstance(offset, int) or not isinstance(length, int):
                continue
            value = self._utf16_slice(text, offset, length).casefold()
            if entity_type == "mention" and value == f"@{self.bot_username}":
                return True
            if entity_type == "bot_command" and (
                "@" not in value or value.endswith(f"@{self.bot_username}")
            ):
                return True
        return False

    @staticmethod
    def _utf16_slice(text: str, offset: int, length: int) -> str:
        encoded = text.encode("utf-16-le")
        return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")

    @staticmethod
    def _extract_message(update: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
        message = update.get("message")
        if isinstance(message, dict):
            return message, False
        edited = update.get("edited_message")
        if isinstance(edited, dict):
            return edited, True
        return None

    @staticmethod
    def _update_id(update: dict[str, Any]) -> int:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise ValueError("Telegram update has no integer update_id")
        return update_id

    def _display_name(self, author: dict[str, Any]) -> str:
        names = [author.get("first_name"), author.get("last_name")]
        display_name = " ".join(value for value in names if isinstance(value, str)).strip()
        inspected = self.privacy.inspect(display_name or "Unknown member")
        if not inspected.accepted or not inspected.sanitized_text:
            return "Unknown member"
        return inspected.sanitized_text

    @staticmethod
    def _attachment_metadata(
        message: dict[str, Any], has_caption: bool
    ) -> list[AttachmentMetadata]:
        for field, media_type in (
            ("photo", "photo"),
            ("video", "video"),
            ("video_note", "video"),
            ("document", "file"),
            ("audio", "audio"),
            ("voice", "audio"),
            ("animation", "animation"),
            ("sticker", "sticker"),
        ):
            if field in message:
                return [AttachmentMetadata(media_type=media_type, has_caption=has_caption)]
        return []
