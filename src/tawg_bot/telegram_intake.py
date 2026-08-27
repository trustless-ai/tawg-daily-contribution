"""Durable L1 ingestion of all supported messages from one Telegram group."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from tawg_bot.aliases import AliasRegistry
from tawg_bot.ids import telegram_id
from tawg_bot.models import (
    AttachmentMetadata,
    DeliveryAttempt,
    DeliveryStatus,
    JobStatus,
    PendingBotJob,
    Relation,
    SourceCursors,
    SourceRecord,
    SourceType,
    TriggerKind,
)
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.query import SourceQuery
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
    _LEGACY_DIRECT_REPLY_BACKFILLS: ClassVar[dict[str, tuple[str, str]]] = {
        "tg:tawg:3470": (
            "2110c0c18a6ce873a957d9b18cbf577b85fe7c2d2bc18cfe874db14231c06e70",
            "tg:tawg:3467",
        )
    }
    _ENGLISH_GREETING = re.compile(
        r"(?<!\w)(?:hello|hi|hey|yo|greetings|welcome|gm|gn|good\s+(?:morning|"
        r"afternoon|evening|night|day)|morning|afternoon|evening|how\s+are\s+you|"
        r"how(?:'|\u2019)?s\s+it\s+going|how\s+is\s+it\s+going|"
        r"what(?:'|\u2019)?s\s+up|"
        r"whats\s+up|sup|hola|bonjour|bonsoir|salut|hallo|guten\s+(?:morgen|tag|"
        r"abend)|ciao|buongiorno|buonasera|olá|bom\s+dia|boa\s+(?:tarde|noite)|"
        r"buenos\s+días|buenas\s+(?:tardes|noches)|namaste|salaam|shalom|"
        r"konnichiwa|ohayo|привет|здравствуйте|доброе\s+утро|добрый\s+(?:день|"
        r"вечер))(?!\w)",
        re.IGNORECASE,
    )
    _CJK_AND_OTHER_GREETING = re.compile(
        r"你好|您好|嗨|哈喽|哈啰|嘿|早|早安|早上好|上午好|中午好|下午好|晚上好|"
        r"晚安|大家好|各位好|你好吗|最近好吗|こんにちは|おはよう(?:ございます)?|"
        r"こんばんは|안녕하세요|좋은\s*아침|مرحبا|السلام\s+عليكم|صباح\s+الخير|"
        r"مساء\s+الخير|नमस्ते|सुप्रभात"
    )

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
        next_offset = (
            max(self._update_id(update) for update in updates) + 1
            if updates
            else cursors.telegram_offset
        )
        records_by_id: dict[str, SourceRecord] = {}
        existing_records = {
            record.record_id: record
            for record in SourceQuery(self.root).records()
            if record.source_type is SourceType.TELEGRAM_MESSAGE
            and record.record_id.startswith(f"tg:{self.group_slug}:")
        }
        jobs_by_id = self._load_jobs()
        initial_job_ids = set(jobs_by_id)
        delivered_bot_message_ids = self._delivered_bot_message_ids()
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
            previous_record = records_by_id.get(record.record_id) or existing_records.get(
                record.record_id
            )
            records_by_id[record.record_id] = record
            job_id = f"reply:{record.record_id}"
            existing = jobs_by_id.get(job_id)
            trigger_kind = self._trigger_kind(message, delivered_bot_message_ids)
            triggers_reply = trigger_kind is not None
            if (
                edited
                and not triggers_reply
                and existing is not None
                and existing.status not in {JobStatus.DELIVERED, JobStatus.IGNORED}
            ):
                del jobs_by_id[job_id]
            elif triggers_reply:
                message_thread_id = self._message_thread_id(message)
                if existing is None:
                    jobs_by_id[job_id] = PendingBotJob(
                        job_id=job_id,
                        trigger_record_id=record.record_id,
                        reply_to_message_id=int(message["message_id"]),
                        message_thread_id=message_thread_id,
                        trigger_kind=trigger_kind,
                        created_at=now,
                        updated_at=now,
                    )
                elif (
                    existing.status is not JobStatus.DELIVERED
                    and (
                        edited
                        or (
                            previous_record is not None
                            and (
                                previous_record.content_sha256 != record.content_sha256
                                or previous_record.source_payload.get("message_thread_id")
                                != record.source_payload.get("message_thread_id")
                            )
                        )
                    )
                ):
                    jobs_by_id[job_id] = existing.model_copy(
                        update={
                            "message_thread_id": message_thread_id,
                            "trigger_kind": trigger_kind,
                            "status": JobStatus.PENDING,
                            "prepared_reply_text": None,
                            "prepared_citations": [],
                            "prepared_language": None,
                            "refusal": False,
                            "safe_error_code": None,
                            "classified_route": None,
                            "router_context_sha256": None,
                            "router_version": None,
                            "routed_at": None,
                            "updated_at": now,
                        }
                    )
                elif existing.message_thread_id is None and message_thread_id is not None:
                    jobs_by_id[job_id] = existing.model_copy(
                        update={"message_thread_id": message_thread_id, "updated_at": now}
                    )

        self._reconcile_direct_replies(
            records={**existing_records, **records_by_id},
            jobs=jobs_by_id,
            delivered_bot_message_ids=delivered_bot_message_ids,
            now=now,
        )

        if not updates and set(jobs_by_id) == initial_job_ids:
            return IntakeResult(0, 0, 0, 0, 0, cursors.telegram_offset, ())

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
            source_payload={
                "message_kind": "group_message",
                "edited": edited,
                "message_thread_id": self._message_thread_id(message),
                "author_is_bot": author_mapping.get("is_bot") is True,
            },
        )

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        raw = json.loads(
            (self.root / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
        )
        jobs = [PendingBotJob.model_validate(item) for item in raw]
        return {job.job_id: job for job in jobs}

    def _trigger_kind(
        self,
        message: dict[str, Any],
        delivered_bot_message_ids: frozenset[int],
    ) -> TriggerKind | None:
        author = message.get("from")
        if isinstance(author, dict) and author.get("is_bot") is True:
            return None
        reply = message.get("reply_to_message")
        if (
            isinstance(reply, dict)
            and isinstance(reply.get("message_id"), int)
            and reply["message_id"] in delivered_bot_message_ids
        ):
            return TriggerKind.REPLY_TO_BOT
        if self._has_explicit_mention(message):
            return TriggerKind.MENTION
        text = message.get("text", message.get("caption", ""))
        if isinstance(text, str) and self._is_greeting_candidate(text):
            return TriggerKind.GREETING_CANDIDATE
        return None

    def _has_explicit_mention(self, message: dict[str, Any]) -> bool:
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
            if entity_type == "bot_command" and value.endswith(f"@{self.bot_username}"):
                return True
        return False

    @classmethod
    def _is_greeting_candidate(cls, text: str) -> bool:
        return (
            cls._ENGLISH_GREETING.search(text) is not None
            or cls._CJK_AND_OTHER_GREETING.search(text) is not None
        )

    def _delivered_bot_message_ids(self) -> frozenset[int]:
        path = self.root / "data/state/delivery-state.json"
        if not path.exists():
            return frozenset()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("invalid Telegram delivery state")
        attempts = [DeliveryAttempt.model_validate(item) for item in raw]
        return frozenset(
            message_id
            for attempt in attempts
            if attempt.status is DeliveryStatus.DELIVERED
            and attempt.telegram_chat_id == self.chat_id
            for message_id in attempt.telegram_message_ids
        )

    def _reconcile_direct_replies(
        self,
        *,
        records: dict[str, SourceRecord],
        jobs: dict[str, PendingBotJob],
        delivered_bot_message_ids: frozenset[int],
        now: datetime,
    ) -> None:
        prefix = f"tg:{self.group_slug}:"
        for record in records.values():
            job_id = f"reply:{record.record_id}"
            if (
                job_id in jobs
                or record.source_payload.get("author_is_bot") is True
            ):
                continue
            reply_targets = [
                relation.target_record_id
                for relation in record.relations
                if relation.relation_type == "reply_to"
            ]
            if not reply_targets:
                continue
            target = reply_targets[0]
            author_is_bot = record.source_payload.get("author_is_bot")
            if author_is_bot is not False and not self._authorized_legacy_backfill(
                record, target
            ):
                continue
            target_id = target.rsplit(":", 1)[-1]
            message_id = record.record_id.removeprefix(prefix)
            if (
                not target_id.isdigit()
                or int(target_id) not in delivered_bot_message_ids
                or not message_id.isdigit()
                or int(message_id) in delivered_bot_message_ids
            ):
                continue
            thread_id = record.source_payload.get("message_thread_id")
            jobs[job_id] = PendingBotJob(
                job_id=job_id,
                trigger_record_id=record.record_id,
                reply_to_message_id=int(message_id),
                message_thread_id=(
                    thread_id
                    if isinstance(thread_id, int) and not isinstance(thread_id, bool)
                    else None
                ),
                trigger_kind=TriggerKind.REPLY_TO_BOT,
                created_at=now,
                updated_at=now,
            )

    @classmethod
    def _authorized_legacy_backfill(
        cls,
        record: SourceRecord,
        target_record_id: str,
    ) -> bool:
        expected = cls._LEGACY_DIRECT_REPLY_BACKFILLS.get(record.record_id)
        return expected == (record.content_sha256, target_record_id)

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

    @staticmethod
    def _message_thread_id(message: dict[str, Any]) -> int | None:
        value = message.get("message_thread_id")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

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
