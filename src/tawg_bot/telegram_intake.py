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

from pydantic import ValidationError

from tawg_bot.aliases import AliasError, AliasRegistry
from tawg_bot.bot_identity import load_webhook_receipts, webhook_receipt_relative_path
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
from tawg_bot.persist_mode import PersistMode
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

_MEMBER_WELCOME = re.compile(
    r"(?<!\w)(?:welcome|glad\s+to\s+have\s+you|great\s+to\s+have\s+you|"
    r"happy\s+to\s+have\s+you|nice\s+to\s+have\s+you)(?!\w)|"
    r"欢迎|歡迎|ようこそ|환영",
    re.IGNORECASE,
)
_PUBLIC_TELEGRAM_HANDLE = re.compile(r"(?<!\w)@([A-Za-z][A-Za-z0-9_]{4,31})(?!\w)")
_WELCOME_TARGET_MAX_AGE_SECONDS = 24 * 60 * 60


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
    reply_to_message_text: str | None
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
        bot_id: int | None = None,
        persist_mode: PersistMode = PersistMode.FULL,
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
        if persist_mode is PersistMode.RECEIPT_ONLY:
            fresh_messages = tuple(incoming_by_id.values())
        else:
            fresh_messages = tuple(
                message
                for message in incoming_by_id.values()
                if _message_supersedes_record(
                    message, persisted_by_id.get(message.record_id)
                )
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
            if message.reply_to_message_text is not None:
                source_payload["reply_to_message_text"] = message.reply_to_message_text
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
            job_id = (
                f"reply:{bot_id}:{record.record_id}"
                if persist_mode is PersistMode.RECEIPT_ONLY and bot_id is not None
                else f"reply:{record.record_id}"
            )
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
                            "router_context_scope": None,
                            "router_context_sha256": None,
                            "router_version": None,
                            "routed_at": None,
                            "knowledge_mutation_paths": [],
                            "knowledge_mutation_trigger_sha256": None,
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

        combined_records = {**persisted_by_id, **records_by_id}
        _invalidate_edited_member_welcomes(
            jobs=jobs_by_id,
            edited_record_ids={
                message.record_id for message in fresh_messages if message.edited
            },
        )

        self._reconcile_direct_replies(
            records=combined_records,
            jobs=jobs_by_id,
            now=now,
        )
        _reconcile_member_welcome_jobs(
            aliases=self.aliases,
            records=combined_records,
            jobs=jobs_by_id,
            now=now,
            remove_pending_generic=True,
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
            [jobs_by_id[job_id].persistence_payload() for job_id in sorted(jobs_by_id)],
        )
        if cursors is not None:
            uow.stage_json("data/state/source-cursors.json", cursors.model_dump(mode="json"))
        if receipts is not None:
            uow.stage_json(
                webhook_receipt_relative_path(bot_id),
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


class MemberWelcomeReconciler:
    """Boundedly backfill two-stage welcomes from durable Telegram evidence."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def reconcile(self, *, now: datetime) -> int:
        _require_utc(now, "member welcome reconciliation time")
        jobs_path = self.root / "data/state/pending-bot-jobs.json"
        aliases_path = self.root / "knowledge/meta/aliases.yml"
        if not jobs_path.exists() or not aliases_path.exists():
            return 0
        raw = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = {
            job.job_id: job
            for job in (PendingBotJob.model_validate(item) for item in raw)
        }
        initial_ids = set(jobs)
        changed = _reconcile_member_welcome_jobs(
            aliases=AliasRegistry.from_yaml(aliases_path),
            records={record.record_id: record for record in TelegramQuery(self.root).records()},
            jobs=jobs,
            now=now,
            remove_pending_generic=True,
        )
        created = len(set(jobs) - initial_ids)
        if not changed:
            return 0
        uow = RepositoryUnitOfWork(
            self.root,
            operation_id=f"member-welcome-reconcile:{int(now.timestamp())}",
        )
        uow.register_external_evidence(())
        uow.stage_json(
            "data/state/pending-bot-jobs.json",
            [jobs[job_id].persistence_payload() for job_id in sorted(jobs)],
        )
        uow.publish()
        return created


def _reconcile_member_welcome_jobs(
    *,
    aliases: AliasRegistry,
    records: dict[str, SourceRecord],
    jobs: dict[str, PendingBotJob],
    now: datetime,
    remove_pending_generic: bool,
) -> bool:
    changed = False
    candidates = sorted(
        (
            job
            for job in jobs.values()
            if job.trigger_kind is TriggerKind.GREETING_CANDIDATE
            and (trigger := records.get(job.trigger_record_id)) is not None
            and _MEMBER_WELCOME.search(trigger.text_original) is not None
            and 0
            <= (now - trigger.created_at).total_seconds()
            <= _WELCOME_TARGET_MAX_AGE_SECONDS
        ),
        key=lambda job: (
            records[job.trigger_record_id].created_at,
            job.trigger_record_id,
        ),
    )
    for candidate in candidates:
        trigger = records[candidate.trigger_record_id]
        resolved = resolve_member_welcome_target(
            trigger=trigger,
            aliases=aliases,
            records=records,
            message_thread_id=candidate.message_thread_id,
        )
        if resolved is None:
            if remove_pending_generic and candidate.status is JobStatus.PENDING:
                jobs.pop(candidate.job_id, None)
                changed = True
            continue
        target_person_id, target_record_id = resolved
        direct_id = f"member-welcome:{target_person_id}"
        introduction_id = f"member-introduction:{target_person_id}"
        if remove_pending_generic and candidate.status is JobStatus.PENDING:
            jobs.pop(candidate.job_id, None)
            changed = True
        if direct_id not in jobs:
            jobs[direct_id] = PendingBotJob(
                job_id=direct_id,
                trigger_record_id=trigger.record_id,
                reply_to_message_id=None,
                message_thread_id=candidate.message_thread_id,
                trigger_kind=TriggerKind.MEMBER_WELCOME,
                welcome_target_person_id=target_person_id,
                welcome_target_record_id=target_record_id,
                created_at=now,
                updated_at=now,
            )
            changed = True
        if introduction_id not in jobs:
            jobs[introduction_id] = PendingBotJob(
                job_id=introduction_id,
                trigger_record_id=trigger.record_id,
                reply_to_message_id=None,
                message_thread_id=candidate.message_thread_id,
                trigger_kind=TriggerKind.MEMBER_INTRODUCTION,
                welcome_target_person_id=target_person_id,
                welcome_target_record_id=target_record_id,
                prerequisite_job_id=direct_id,
                created_at=now,
                updated_at=now,
            )
            changed = True
    return changed


def _invalidate_edited_member_welcomes(
    *,
    jobs: dict[str, PendingBotJob],
    edited_record_ids: set[str],
) -> None:
    if not edited_record_ids:
        return
    member_kinds = {
        TriggerKind.MEMBER_WELCOME,
        TriggerKind.MEMBER_INTRODUCTION,
    }
    for job_id, job in tuple(jobs.items()):
        if (
            job.trigger_kind not in member_kinds
            or (
                job.trigger_record_id not in edited_record_ids
                and job.welcome_target_record_id not in edited_record_ids
            )
            or job.status is JobStatus.DELIVERED
        ):
            continue
        jobs.pop(job_id, None)


def resolve_member_welcome_target(
    *,
    trigger: SourceRecord,
    aliases: AliasRegistry,
    records: dict[str, SourceRecord],
    message_thread_id: int | None = None,
) -> tuple[str, str] | None:
    reply_targets = [
        relation.target_record_id
        for relation in trigger.relations
        if relation.relation_type == "reply_to"
    ]
    if len(reply_targets) == 1:
        target = records.get(reply_targets[0])
        if (
            target is not None
            and target.source_payload.get("message_thread_id") == message_thread_id
            and _eligible_welcome_target(
                trigger,
                target,
                aliases,
                records,
            )
        ):
            assert target.author_person_id is not None
            return target.author_person_id, target.record_id

    welcome_match = _MEMBER_WELCOME.search(trigger.text_original)
    if welcome_match is None:
        return None
    nearby_text = trigger.text_original[
        max(0, welcome_match.start() - 64) : welcome_match.end() + 96
    ]
    person_ids: set[str] = set()
    for handle in _PUBLIC_TELEGRAM_HANDLE.findall(nearby_text):
        try:
            person_id = aliases.lookup_public_handle("telegram", handle)
        except AliasError:
            return None
        if person_id is not None:
            person_ids.add(person_id)
    for person_id, identity in aliases.people.items():
        for display_name in identity.get("display_names", []):
            if isinstance(display_name, str) and _display_name_is_mentioned(
                nearby_text,
                display_name,
            ):
                person_ids.add(person_id)
                break
    person_ids.discard(trigger.author_person_id or "")
    resolved: list[tuple[str, str]] = []
    for person_id in sorted(person_ids):
        target = _latest_target_record(
            trigger,
            person_id,
            records,
            message_thread_id=message_thread_id,
        )
        if target is not None and _eligible_welcome_target(
            trigger,
            target,
            aliases,
            records,
        ):
            resolved.append((person_id, target.record_id))
    return resolved[0] if len(resolved) == 1 else None


def _latest_target_record(
    trigger: SourceRecord,
    person_id: str,
    records: dict[str, SourceRecord],
    *,
    message_thread_id: int | None,
) -> SourceRecord | None:
    candidates = [
        record
        for record in records.values()
        if record.author_person_id == person_id
        and record.created_at <= trigger.created_at
        and record.source_payload.get("message_thread_id") == message_thread_id
        and (trigger.created_at - record.created_at).total_seconds()
        <= _WELCOME_TARGET_MAX_AGE_SECONDS
    ]
    return max(candidates, key=lambda item: (item.created_at, item.record_id), default=None)


def _eligible_welcome_target(
    trigger: SourceRecord,
    target: SourceRecord,
    aliases: AliasRegistry,
    records: dict[str, SourceRecord],
) -> bool:
    if (
        target.source_type is not SourceType.TELEGRAM_MESSAGE
        or target.author_person_id is None
        or target.author_person_id == trigger.author_person_id
        or target.created_at > trigger.created_at
        or (trigger.created_at - target.created_at).total_seconds()
        > _WELCOME_TARGET_MAX_AGE_SECONDS
    ):
        return False
    identity = aliases.people.get(target.author_person_id, {})
    handles = identity.get("handles", {})
    telegram_handles = handles.get("telegram", []) if isinstance(handles, dict) else []
    return (
        isinstance(telegram_handles, list)
        and len(telegram_handles) == 1
        and isinstance(telegram_handles[0], str)
        and _is_new_tawg_member(trigger, target.author_person_id, aliases, records)
    )


def _is_new_tawg_member(
    trigger: SourceRecord,
    person_id: str,
    aliases: AliasRegistry,
    records: dict[str, SourceRecord],
) -> bool:
    identity = aliases.people.get(person_id, {})
    handles = _identity_public_handles(identity)
    related_ids = {
        candidate_id
        for candidate_id, candidate in aliases.people.items()
        if handles & _identity_public_handles(candidate)
    }
    related_ids.add(person_id)
    first_seen = min(
        (
            record.created_at
            for record in records.values()
            if record.source_type is SourceType.TELEGRAM_MESSAGE
            and record.author_person_id in related_ids
            and record.created_at <= trigger.created_at
        ),
        default=None,
    )
    return (
        first_seen is not None
        and 0
        <= (trigger.created_at - first_seen).total_seconds()
        <= _WELCOME_TARGET_MAX_AGE_SECONDS
    )


def _identity_public_handles(identity: dict[str, Any]) -> set[tuple[str, str]]:
    handles = identity.get("handles", {})
    if not isinstance(handles, dict):
        return set()
    return {
        (platform.casefold(), value.removeprefix("@").casefold())
        for platform, values in handles.items()
        if isinstance(platform, str)
        if isinstance(values, list)
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _display_name_is_mentioned(text: str, display_name: str) -> bool:
    normalized = " ".join(display_name.split())
    if len(normalized) < 2:
        return False
    candidates = [normalized]
    first_name = normalized.split(" ", 1)[0]
    if len(first_name) >= 3 and first_name != normalized:
        candidates.append(first_name)
    for candidate in candidates:
        if any(character.isalnum() and ord(character) < 128 for character in candidate):
            if (
                re.search(
                    rf"(?<!\w){re.escape(candidate)}(?!\w)",
                    text,
                    re.IGNORECASE,
                )
                is not None
            ):
                return True
        elif candidate.casefold() in text.casefold():
            return True
    return False


def ingest_envelopes(
    *,
    root: Path,
    group_slug: str,
    bot_username: str,
    envelopes: Iterable[TelegramWebhookEnvelope],
    now: datetime,
    uow_factory: UnitOfWorkFactory = _default_uow_factory,
    telegram_chat_id: int | None = None,
    bot_id: int | None = None,
    persist_mode: PersistMode = PersistMode.FULL,
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

    receipts = load_webhook_receipts(
        root,
        bot_id=bot_id,
        persist_mode=persist_mode,
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
        bot_id=bot_id,
        persist_mode=persist_mode,
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
        reply_to_message_text = None
        if isinstance(reply, dict):
            raw_reply_text = reply.get("text") or reply.get("caption")
            if isinstance(raw_reply_text, str):
                reply_to_message_text = raw_reply_text
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
            reply_to_message_text=reply_to_message_text,
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
            or _MEMBER_WELCOME.search(text) is not None
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
        reply_to_message_text=envelope.reply_to_message_text,
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
        persist_thread_metadata=True,
    )


def _delivered_bot_message_ids(
    root: Path, *, chat_id: int | None = None
) -> frozenset[int]:
    if chat_id is None:
        return frozenset()
    message_ids: set[int] = set()
    for path in sorted((root / "data/state").glob("delivery-state*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                continue
            attempts = [DeliveryAttempt.model_validate(item) for item in raw]
        except (
            OSError,
            UnicodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ):
            continue
        for attempt in attempts:
            if (
                attempt.status is DeliveryStatus.DELIVERED
                and attempt.telegram_chat_id == chat_id
            ):
                message_ids.update(attempt.telegram_message_ids)
    return frozenset(message_ids)


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
