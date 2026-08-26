"""Retry-safe Telegram delivery with explicit external ambiguity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from tawg_bot.models import (
    DeliveryAttempt,
    DeliveryStatus,
    JobStatus,
    PendingBotJob,
)
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.telegram_api import (
    SentMessage,
    TelegramApiAmbiguousError,
    TelegramApiError,
)
from tawg_bot.telegram_text import TelegramTextSplitError, split_telegram_text
from tawg_bot.unit_of_work import RepositoryUnitOfWork


class DeliveryRejected(ValueError):
    """Raised before Telegram is called when an intent is unsafe."""


class DeliveryFailed(RuntimeError):
    """Raised for an explicit, safely retryable delivery failure."""


class DeliveryAmbiguous(RuntimeError):
    """Raised when automatic retry could duplicate an accepted Telegram message."""


class DeliveryApi(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SentMessage: ...


class DeliveryCheckpoint(Protocol):
    async def publish(self, operation_id: str, root: Path) -> None:
        """Make the current repository state durable before/after the external call."""


class DeliveryService:
    _STATE_PATH = "data/state/delivery-state.json"
    _JOBS_PATH = "data/state/pending-bot-jobs.json"
    _TELEGRAM_LIMIT = 32768
    _MAX_MESSAGES = 2

    def __init__(
        self,
        root: Path,
        *,
        api: DeliveryApi,
        chat_id: int,
        checkpoint: DeliveryCheckpoint,
        after_send_hook: Callable[[], None] | None = None,
    ) -> None:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("configured Telegram chat ID must be an integer")
        self.root = root.resolve()
        self.api = api
        self.chat_id = chat_id
        self.checkpoint = checkpoint
        self.after_send_hook = after_send_hook or (lambda: None)
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def deliver(
        self,
        *,
        job_id: str,
        text: str,
        reply_to_message_id: int | None,
        message_thread_id: int | None = None,
        now: datetime,
    ) -> DeliveryAttempt:
        self._require_utc(now)
        messages = self._split(text)
        try:
            self.privacy.assert_public(text)
        except PrivacyViolation:
            raise DeliveryRejected("delivery text failed privacy validation") from None
        content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        attempts = self._load_attempts()
        existing = attempts.get(job_id)
        if existing is not None and existing.content_sha256 not in {None, content_sha}:
            raise DeliveryFailed("delivery job content changed after intent was recorded")
        if existing is not None and existing.status is DeliveryStatus.DELIVERED:
            return existing
        if existing is not None and existing.status is DeliveryStatus.AMBIGUOUS:
            raise DeliveryAmbiguous("delivery requires operator review")
        if existing is not None and existing.status is DeliveryStatus.SENDING:
            ambiguous = existing.model_copy(
                update={
                    "status": DeliveryStatus.AMBIGUOUS,
                    "updated_at": now,
                    "safe_error_code": "recovered_sending",
                }
            )
            attempts[job_id] = ambiguous
            self._publish_attempts(attempts, f"delivery:{job_id}:ambiguous")
            await self.checkpoint.publish(f"delivery:{job_id}:ambiguous", self.root)
            raise DeliveryAmbiguous("recovered sending delivery requires operator review")

        prepared = DeliveryAttempt(
            delivery_id=job_id,
            job_id=job_id,
            status=DeliveryStatus.PREPARED,
            content_sha256=content_sha,
            message_count=len(messages),
            delivery_format="rich_markdown_v1",
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            prepared_at=existing.prepared_at if existing is not None else now,
            updated_at=now,
        )
        attempts[job_id] = prepared
        self._publish_attempts(attempts, f"delivery:{job_id}:prepared")
        await self.checkpoint.publish(f"delivery:{job_id}:prepared", self.root)

        sending = prepared.model_copy(update={"status": DeliveryStatus.SENDING, "updated_at": now})
        attempts[job_id] = sending
        self._publish_attempts(attempts, f"delivery:{job_id}:sending")
        await self.checkpoint.publish(f"delivery:{job_id}:sending", self.root)
        try:
            self.privacy.assert_public(text)
        except PrivacyViolation:
            raise DeliveryRejected("delivery text failed final privacy validation") from None

        sent: list[SentMessage] = []
        for index, message in enumerate(messages):
            try:
                response = await self.api.send_message(
                    self.chat_id,
                    message,
                    reply_to_message_id if index == 0 else None,
                    message_thread_id,
                )
            except TelegramApiError as error:
                ambiguous_error = isinstance(error, TelegramApiAmbiguousError)
                status = (
                    DeliveryStatus.AMBIGUOUS
                    if sent or ambiguous_error
                    else DeliveryStatus.FAILED
                )
                error_code = (
                    "partial_telegram_failure"
                    if sent
                    else "telegram_outcome_unknown"
                    if ambiguous_error
                    else "telegram_api_failure"
                )
                failed = sending.model_copy(
                    update={
                        "status": status,
                        "telegram_chat_id": self.chat_id if sent else None,
                        "telegram_message_ids": [item.message_id for item in sent],
                        "delivery_format": (
                            self._actual_delivery_format(sent)
                            if sent
                            else sending.delivery_format
                        ),
                        "updated_at": now,
                        "safe_error_code": error_code,
                    }
                )
                attempts[job_id] = failed
                self._publish_attempts(attempts, f"delivery:{job_id}:{status.value}")
                await self.checkpoint.publish(f"delivery:{job_id}:{status.value}", self.root)
                if status is DeliveryStatus.AMBIGUOUS:
                    raise DeliveryAmbiguous("Telegram delivery outcome is unknown") from None
                raise DeliveryFailed("Telegram explicitly rejected the delivery") from None
            if response.chat_id != self.chat_id:
                ambiguous = sending.model_copy(
                    update={
                        "status": DeliveryStatus.AMBIGUOUS,
                        "telegram_chat_id": response.chat_id,
                        "telegram_message_ids": [
                            *[item.message_id for item in sent],
                            response.message_id,
                        ],
                        "delivery_format": self._actual_delivery_format(
                            [*sent, response]
                        ),
                        "updated_at": now,
                        "safe_error_code": "destination_mismatch",
                    }
                )
                attempts[job_id] = ambiguous
                self._publish_attempts(attempts, f"delivery:{job_id}:ambiguous")
                await self.checkpoint.publish(f"delivery:{job_id}:ambiguous", self.root)
                raise DeliveryAmbiguous("Telegram returned an unexpected destination")
            sent.append(response)

        self.after_send_hook()
        delivered = sending.model_copy(
            update={
                "status": DeliveryStatus.DELIVERED,
                "telegram_chat_id": self.chat_id,
                "telegram_message_ids": [item.message_id for item in sent],
                "delivery_format": self._actual_delivery_format(sent),
                "sent_at": now,
                "updated_at": now,
                "safe_error_code": None,
            }
        )
        attempts[job_id] = delivered
        self._publish_final(attempts, delivered)
        await self.checkpoint.publish(f"delivery:{job_id}:delivered", self.root)
        return delivered

    def _split(self, text: str) -> tuple[str, ...]:
        try:
            return split_telegram_text(
                text,
                limit=self._TELEGRAM_LIMIT,
                max_messages=self._MAX_MESSAGES,
            )
        except (TelegramTextSplitError, ValueError):
            raise DeliveryRejected("delivery cannot fit in at most two Telegram messages") from None

    @staticmethod
    def _actual_delivery_format(sent: list[SentMessage]) -> str:
        delivery_formats = {item.delivery_format for item in sent}
        return next(iter(delivery_formats)) if len(delivery_formats) == 1 else "mixed_v1"

    def _load_attempts(self) -> dict[str, DeliveryAttempt]:
        path = self.root / self._STATE_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            attempts = [DeliveryAttempt.model_validate(item) for item in raw]
        except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise DeliveryRejected("invalid delivery state") from error
        by_id = {attempt.delivery_id: attempt for attempt in attempts}
        if len(by_id) != len(attempts):
            raise DeliveryRejected("duplicate delivery intent")
        return by_id

    def _publish_attempts(self, attempts: Mapping[str, DeliveryAttempt], operation_id: str) -> None:
        uow = RepositoryUnitOfWork(self.root, operation_id=operation_id)
        uow.register_external_evidence(())
        self._stage_attempts(uow, attempts)
        uow.publish()

    def _publish_final(
        self, attempts: Mapping[str, DeliveryAttempt], delivered: DeliveryAttempt
    ) -> None:
        uow = RepositoryUnitOfWork(self.root, operation_id=f"delivery:{delivered.job_id}:delivered")
        uow.register_external_evidence(())
        self._stage_attempts(uow, attempts)
        jobs = self._load_jobs_if_present()
        job = jobs.get(delivered.job_id)
        if job is not None:
            jobs[delivered.job_id] = job.model_copy(
                update={
                    "status": JobStatus.DELIVERED,
                    "updated_at": delivered.updated_at,
                    "safe_error_code": None,
                }
            )
            uow.stage_json(
                self._JOBS_PATH,
                [jobs[job_id].model_dump(mode="json") for job_id in sorted(jobs)],
            )
        uow.publish()

    def _load_jobs_if_present(self) -> dict[str, PendingBotJob]:
        path = self.root / self._JOBS_PATH
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            jobs = [PendingBotJob.model_validate(item) for item in raw]
        except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise DeliveryRejected("invalid pending job state") from error
        return {job.job_id: job for job in jobs}

    @staticmethod
    def _stage_attempts(uow: RepositoryUnitOfWork, attempts: Mapping[str, DeliveryAttempt]) -> None:
        serialized: list[dict[str, object]] = []
        for key in sorted(attempts):
            item = attempts[key].model_dump(mode="json")
            if item.get("delivery_format") is None:
                item.pop("delivery_format", None)
            serialized.append(item)
        uow.stage_json(
            DeliveryService._STATE_PATH,
            serialized,
        )

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("delivery time must use UTC")
