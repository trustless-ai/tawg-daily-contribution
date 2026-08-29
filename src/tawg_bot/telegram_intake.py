"""Durable L1 ingestion of Telegram polling updates and webhook envelopes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
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
    TelegramWebhookReceipts,
    TriggerKind,
)
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.query import TelegramQuery
from tawg_bot.storage import partition_stable_records
from tawg_bot.telegram_webhook import (
    TelegramWebhookEnvelope,
    telegram_entities_trigger_reply,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class TelegramUpdateApi(Protocol):
    async def get_all_updates(self, offset: int, limit: int = 100) -> list[dict[str, Any]]: ...


UnitOfWorkFactory = Callable[[Path, str], RepositoryUnitOfWork]

_LEGACY_DIRECT_REPLY_BACKFILLS: dict[str, tuple[str, str]] = {
    "tg:tawg:3470": (
        "2110c0c18a6ce873a957d9b18cbf577b85fe7c2d2bc18cfe874db14231c06e70",
        "tg:tawg:3467",
    )
}


@dataclass(frozen=True, slots=True)
class IntakeResult:
    received: int
    persisted: int
    filtered: int
    rejected: int
    jobs_created: int
    next_offset: int
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WebhookIntakeResult:
    received: int
    persisted: int
    replayed: int
    jobs_created: int
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TelegramMessage:
    update_id: int
    record_id: str
    message_id: int
    created_at: datetime
    updated_at: datetime
    text: str
    public_username: str | None
    display_name: str
    reply_to_message_id: int | None
    message_thread_id: int | None
    attachments: tuple[AttachmentMetadata, ...]
    edited: bool
    trigger_kind: TriggerKind | None
    author_is_bot: bool | None
    persist_thread_metadata: bool


@dataclass(frozen=True, slots=True)
class _PersistenceResult:
    persisted: int
    jobs_created: int
    changed_paths: tuple[str, ...]


def _default_uow_factory(root: Path, operation_id: str) -> RepositoryUnitOfWork:
    return RepositoryUnitOfWork(root, operation_id=operation_id)


class _TelegramPersistence:
    """Common stable-record and pending-job publication for all Telegram inputs."""

    def __init__(
        self,
        *,
        root: Path,
        group_slug: str,
        aliases: AliasRegistry,
        uow_factory: UnitOfWorkFactory,
        chat_id: int | None = None,
    ) -> None:
        self.root = root
        self.group_slug = group_slug
        self.aliases = aliases
        self.uow_factory = uow_factory
        self.chat_id = chat_id

    def persist(
        self,
        messages: Iterable[_TelegramMessage],
        *,
        now: datetime,
        cursors: SourceCursors | None = None,
        receipts: TelegramWebhookReceipts | None = None,
    ) -> _PersistenceResult:
        all_messages = tuple(messages)
        incoming_by_id: dict[str, _TelegramMessage] = {}
        for message in all_messages:
            current = incoming_by_id.get(message.record_id)
            if current is None or _message_is_fresher(
                updated_at=message.updated_at,
                edited=message.edited,
                update_id=message.update_id,
                current_updated_at=current.updated_at,
                current_edited=current.edited,
                current_update_id=current.update_id,
            ):
                incoming_by_id[message.record_id] = message
        persisted_by_id = {
            record.record_id: record for record in TelegramQuery(self.root).records()
        }
        fresh_messages = tuple(
            message
            for message in incoming_by_id.values()
            if _message_supersedes_record(message, persisted_by_id.get(message.record_id))
        )

        records_by_id: dict[str, SourceRecord] = {}
        jobs_by_id = self._load_jobs()
        initial_job_ids = set(jobs_by_id)
        for message in fresh_messages:
            person_id = self.aliases.resolve_telegram_live(
                public_username=message.public_username,
                display_name=message.display_name,
            )
            month_path = f"data/telegram/{message.created_at:%Y/%m}/messages.jsonl"
            relations = (
                [
                    Relation(
                        relation_type="reply_to",
                        target_record_id=telegram_id(
                            self.group_slug, message.reply_to_message_id
                        ),
                    )
                ]
                if message.reply_to_message_id is not None
                else []
            )
            source_payload: dict[str, Any] = {
                "message_kind": "group_message",
                "edited": message.edited,
                "update_id": message.update_id,
            }
            if message.author_is_bot is not None:
                source_payload["author_is_bot"] = message.author_is_bot
            if message.persist_thread_metadata:
                source_payload["message_thread_id"] = message.message_thread_id
            record = SourceRecord.from_text(
                record_id=message.record_id,
                source_type=SourceType.TELEGRAM_MESSAGE,
                source_locator=f"repo:{month_path}#{message.record_id}",
                author_person_id=person_id,
                author_source_handle=(
                    f"@{message.public_username}"
                    if message.public_username
                    else message.display_name
                ),
                created_at=message.created_at,
                updated_at=message.updated_at,
                text_original=message.text,
                relations=relations,
                attachment_metadata=list(message.attachments),
                ingested_at=now,
                source_payload=source_payload,
            )
            records_by_id[record.record_id] = record
            job_id = f"reply:{record.record_id}"
            existing = jobs_by_id.get(job_id)
            previous_record = persisted_by_id.get(record.record_id)
            if (
                message.edited
                and message.trigger_kind is None
                and existing is not None
                and existing.status not in {JobStatus.DELIVERED, JobStatus.IGNORED}
            ):
                del jobs_by_id[job_id]
            elif message.trigger_kind is not None:
                if existing is None:
                    jobs_by_id[job_id] = PendingBotJob(
                        job_id=job_id,
                        trigger_record_id=record.record_id,
                        reply_to_message_id=message.message_id,
                        message_thread_id=message.message_thread_id,
                        trigger_kind=message.trigger_kind,
                        created_at=now,
                        updated_at=now,
                    )
                elif (
                    existing.status is not JobStatus.DELIVERED
                    and (
                        message.edited
                        or (
                            previous_record is not None
                            and (
                                previous_record.content_sha256 != record.content_sha256
                                or existing.message_thread_id
                                != message.message_thread_id
                            )
                        )
                    )
                ):
                    jobs_by_id[job_id] = existing.model_copy(
                        update={
                            "message_thread_id": message.message_thread_id,
                            "trigger_kind": message.trigger_kind,
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
                elif (
                    existing.message_thread_id is None
                    and message.message_thread_id is not None
                ):
                    jobs_by_id[job_id] = existing.model_copy(
                        update={
                            "message_thread_id": message.message_thread_id,
                            "updated_at": now,
                        }
                    )

        for message in all_messages:
            if message.trigger_kind is None or message.message_thread_id is None:
                continue
            job_id = f"reply:{message.record_id}"
            existing = jobs_by_id.get(job_id)
            if existing is not None and existing.message_thread_id is None:
                jobs_by_id[job_id] = existing.model_copy(
                    update={
                        "message_thread_id": message.message_thread_id,
                        "updated_at": now,
                    }
                )

        self._reconcile_direct_replies(
            records={**persisted_by_id, **records_by_id},
            jobs=jobs_by_id,
            now=now,
        )

        monthly: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in records_by_id.values():
            monthly[f"data/telegram/{record.created_at:%Y/%m}/messages.jsonl"].append(
                record
            )

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
        if cursors is not None:
            uow.stage_json("data/state/source-cursors.json", cursors.model_dump(mode="json"))
        if receipts is not None:
            uow.stage_json(
                "data/state/telegram-webhook-receipts.json",
                receipts.model_dump(mode="json"),
            )
        uow.stage_bytes("knowledge/meta/aliases.yml", self.aliases.to_yaml_bytes())
        changed_paths = uow.publish().changed_paths
        return _PersistenceResult(
            persisted=len(records_by_id),
            jobs_created=len(set(jobs_by_id) - initial_job_ids),
            changed_paths=changed_paths,
        )

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        raw = json.loads(
            (self.root / "data/state/pending-bot-jobs.json").read_text(encoding="utf-8")
        )
        jobs = [PendingBotJob.model_validate(item) for item in raw]
        return {job.job_id: job for job in jobs}

    def _reconcile_direct_replies(
        self,
        *,
        records: dict[str, SourceRecord],
        jobs: dict[str, PendingBotJob],
        now: datetime,
    ) -> None:
        delivered_bot_message_ids = _delivered_bot_message_ids(
            self.root, chat_id=self.chat_id
        )
        prefix = f"tg:{self.group_slug}:"
        for record in records.values():
            if not record.record_id.startswith(prefix):
                continue
            job_id = f"reply:{record.record_id}"
            if job_id in jobs or record.source_payload.get("author_is_bot") is True:
                continue
            reply_targets = [
                relation.target_record_id
                for relation in record.relations
                if relation.relation_type == "reply_to"
            ]
            if not reply_targets:
                continue
            target = reply_targets[0]
            if not target.startswith(prefix):
                continue
            author_is_bot = record.source_payload.get("author_is_bot")
            if author_is_bot is not False and not _authorized_legacy_backfill(
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


def ingest_envelopes(
    *,
    root: Path,
    group_slug: str,
    bot_username: str,
    envelopes: Iterable[TelegramWebhookEnvelope],
    now: datetime,
    uow_factory: UnitOfWorkFactory = _default_uow_factory,
    telegram_chat_id: int | None = None,
) -> WebhookIntakeResult:
    """Verify and atomically ingest sanitized webhook envelopes without polling."""
    _require_utc(now, "ingestion time")
    batch = tuple(envelopes)
    for envelope in batch:
        _verify_envelope(
            envelope,
            group_slug=group_slug,
            bot_username=bot_username,
        )

    receipts_path = root / "data/state/telegram-webhook-receipts.json"
    receipts = (
        TelegramWebhookReceipts.model_validate_json(
            receipts_path.read_text(encoding="utf-8")
        )
        if receipts_path.exists()
        else TelegramWebhookReceipts(
            schema_version="tawg.telegram-webhook-receipts.v1"
        )
    )
    seen = set(receipts.update_ids)
    unseen: list[TelegramWebhookEnvelope] = []
    for envelope in batch:
        if envelope.update_id in seen:
            continue
        seen.add(envelope.update_id)
        unseen.append(envelope)
    if not unseen:
        return WebhookIntakeResult(
            received=len(batch),
            persisted=0,
            replayed=len(batch),
            jobs_created=0,
            changed_paths=(),
        )

    updated_receipts = TelegramWebhookReceipts(
        schema_version="tawg.telegram-webhook-receipts.v1",
        update_ids=[*receipts.update_ids, *(item.update_id for item in unseen)]
    )
    aliases = AliasRegistry.from_yaml(root / "knowledge/meta/aliases.yml")
    persistence = _TelegramPersistence(
        root=root,
        group_slug=group_slug,
        aliases=aliases,
        uow_factory=uow_factory,
        chat_id=telegram_chat_id,
    )
    delivered_bot_message_ids = _delivered_bot_message_ids(
        root, chat_id=telegram_chat_id
    )
    result = persistence.persist(
        (
            _message_from_envelope(
                item, delivered_bot_message_ids=delivered_bot_message_ids
            )
            for item in unseen
        ),
        now=now,
        receipts=updated_receipts,
    )
    return WebhookIntakeResult(
        received=len(batch),
        persisted=result.persisted,
        replayed=len(batch) - len(unseen),
        jobs_created=result.jobs_created,
        changed_paths=result.changed_paths,
    )


class TelegramIntake:
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
        _require_utc(now, "collection time")
        cursors = SourceCursors.model_validate_json(
            (self.root / "data/state/source-cursors.json").read_text(encoding="utf-8")
        )
        updates = await self.api.get_all_updates(cursors.telegram_offset)
        next_offset = (
            max(self._update_id(update) for update in updates) + 1
            if updates
            else cursors.telegram_offset
        )
        messages: list[_TelegramMessage] = []
        delivered_bot_message_ids = _delivered_bot_message_ids(
            self.root, chat_id=self.chat_id
        )
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
            converted = self._message_from_polling(
                message,
                edited=edited,
                update_id=self._update_id(update),
                delivered_bot_message_ids=delivered_bot_message_ids,
            )
            if converted is None:
                rejected += 1
                continue
            messages.append(converted)

        cursors.telegram_offset = next_offset
        result = _TelegramPersistence(
            root=self.root,
            group_slug=self.group_slug,
            aliases=self.aliases,
            uow_factory=self.uow_factory,
            chat_id=self.chat_id,
        ).persist(messages, now=now, cursors=cursors)
        return IntakeResult(
            received=len(updates),
            persisted=result.persisted,
            filtered=filtered,
            rejected=rejected,
            jobs_created=result.jobs_created,
            next_offset=next_offset,
            changed_paths=result.changed_paths,
        )

    def _message_from_polling(
        self,
        message: dict[str, Any],
        *,
        edited: bool,
        update_id: int,
        delivered_bot_message_ids: frozenset[int],
    ) -> _TelegramMessage | None:
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
        created_at = datetime.fromtimestamp(created_timestamp, tz=UTC)
        edit_timestamp = message.get("edit_date")
        updated_at = (
            datetime.fromtimestamp(edit_timestamp, tz=UTC)
            if isinstance(edit_timestamp, int)
            else created_at
        )
        reply = message.get("reply_to_message")
        reply_to_message_id = (
            reply.get("message_id")
            if isinstance(reply, dict) and isinstance(reply.get("message_id"), int)
            else None
        )
        return _TelegramMessage(
            update_id=update_id,
            record_id=telegram_id(self.group_slug, message_id),
            message_id=message_id,
            created_at=created_at,
            updated_at=updated_at,
            text=inspected.sanitized_text,
            public_username=public_username,
            display_name=self._display_name(author_mapping),
            reply_to_message_id=reply_to_message_id,
            message_thread_id=self._message_thread_id(message),
            attachments=tuple(self._attachment_metadata(message, bool(text))),
            edited=edited,
            trigger_kind=self._trigger_kind(message, delivered_bot_message_ids),
            author_is_bot=(
                author_mapping.get("is_bot")
                if isinstance(author_mapping.get("is_bot"), bool)
                else None
            ),
            persist_thread_metadata=True,
        )

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
        if self._has_bot_command(message):
            return None
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
        return False

    @staticmethod
    def _has_bot_command(message: dict[str, Any]) -> bool:
        entities = message.get("entities", message.get("caption_entities", []))
        return isinstance(entities, list) and any(
            isinstance(entity, dict) and entity.get("type") == "bot_command"
            for entity in entities
        )

    @classmethod
    def _is_greeting_candidate(cls, text: str) -> bool:
        return (
            cls._ENGLISH_GREETING.search(text) is not None
            or cls._CJK_AND_OTHER_GREETING.search(text) is not None
        )

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


def _verify_envelope(
    envelope: TelegramWebhookEnvelope, *, group_slug: str, bot_username: str
) -> None:
    if envelope.schema_version != "tawg.telegram-webhook-envelope.v1":
        raise ValueError("Telegram webhook envelope schema is invalid")
    if envelope.source_id != telegram_id(group_slug, envelope.message_id):
        raise ValueError("Telegram webhook envelope source identity is invalid")
    if envelope.edited != (envelope.edited_timestamp is not None):
        raise ValueError("Telegram webhook envelope edit metadata is invalid")
    payload = envelope.model_dump(exclude={"integrity_digest"}, mode="json")
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not hmac.compare_digest(envelope.integrity_digest, expected):
        raise ValueError("Telegram webhook envelope integrity check failed")
    if (
        any(entity.entity_type == "bot_command" for entity in envelope.entities)
        and not envelope.has_bot_command
    ):
        raise ValueError("Telegram webhook envelope command metadata is invalid")
    expected_trigger = telegram_entities_trigger_reply(
        envelope.entities,
        bot_username=bot_username,
    )
    if envelope.triggers_reply is not expected_trigger:
        raise ValueError("Telegram webhook envelope trigger metadata is invalid")


def _message_from_envelope(
    envelope: TelegramWebhookEnvelope,
    *,
    delivered_bot_message_ids: frozenset[int],
) -> _TelegramMessage:
    created_at = datetime.fromtimestamp(envelope.timestamp, tz=UTC)
    updated_at = (
        datetime.fromtimestamp(envelope.edited_timestamp, tz=UTC)
        if envelope.edited_timestamp is not None
        else created_at
    )
    trigger_kind: TriggerKind | None = None
    if not envelope.author_is_bot:
        if envelope.reply_to_message_id in delivered_bot_message_ids:
            trigger_kind = TriggerKind.REPLY_TO_BOT
        elif envelope.triggers_reply:
            trigger_kind = TriggerKind.MENTION
        elif envelope.has_bot_command:
            trigger_kind = None
        elif TelegramIntake._is_greeting_candidate(envelope.text):
            trigger_kind = TriggerKind.GREETING_CANDIDATE
    return _TelegramMessage(
        update_id=envelope.update_id,
        record_id=envelope.source_id,
        message_id=envelope.message_id,
        created_at=created_at,
        updated_at=updated_at,
        text=envelope.text,
        public_username=envelope.public_username,
        display_name=envelope.display_name,
        reply_to_message_id=envelope.reply_to_message_id,
        message_thread_id=envelope.message_thread_id,
        attachments=tuple(
            AttachmentMetadata(
                media_type=attachment.media_type,
                has_caption=attachment.has_caption,
            )
            for attachment in envelope.attachments
        ),
        edited=envelope.edited,
        trigger_kind=trigger_kind,
        author_is_bot=envelope.author_is_bot,
        persist_thread_metadata=False,
    )


def _delivered_bot_message_ids(
    root: Path, *, chat_id: int | None = None
) -> frozenset[int]:
    if chat_id is None:
        return frozenset()
    path = root / "data/state/delivery-state.json"
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
        and attempt.telegram_chat_id == chat_id
        for message_id in attempt.telegram_message_ids
    )


def _authorized_legacy_backfill(
    record: SourceRecord,
    target_record_id: str,
) -> bool:
    expected = _LEGACY_DIRECT_REPLY_BACKFILLS.get(record.record_id)
    return expected == (record.content_sha256, target_record_id)


def _message_supersedes_record(
    message: _TelegramMessage,
    record: SourceRecord | None,
) -> bool:
    if record is None:
        return True
    return _message_is_fresher(
        updated_at=message.updated_at,
        edited=message.edited,
        update_id=message.update_id,
        current_updated_at=record.updated_at,
        current_edited=record.source_payload.get("edited") is True,
        current_update_id=_record_update_id(record),
    )


def _message_is_fresher(
    *,
    updated_at: datetime,
    edited: bool,
    update_id: int,
    current_updated_at: datetime,
    current_edited: bool,
    current_update_id: int | None,
) -> bool:
    if updated_at != current_updated_at:
        return updated_at > current_updated_at
    if edited != current_edited:
        return edited
    if current_update_id is None:
        return False
    return update_id > current_update_id


def _record_update_id(record: SourceRecord) -> int | None:
    value = record.source_payload.get("update_id")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must use UTC")
