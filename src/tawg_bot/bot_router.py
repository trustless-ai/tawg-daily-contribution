"""Permission-first routing and grounded preparation for Telegram mentions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import ValidationError

from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.corrections import CorrectionService
from tawg_bot.models import JobStatus, PendingBotJob, SourceRecord, StrictModel
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault_transaction import VaultTransaction, VaultTransactionEngine

_NON_ENGLISH = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\u0400-\u052f\u0600-\u06ff\u0900-\u097f]"
)


class BotRoute(StrEnum):
    KNOWLEDGE_QUESTION = "knowledge_question"
    IDENTITY_CORRECTION = "identity_correction"
    KNOWLEDGE_CORRECTION = "knowledge_correction"
    SOURCE_SUGGESTION = "source_suggestion"
    REFUSE = "refuse"


class ReplyRejected(ValueError):
    """Raised when a mention or generated reply cannot safely be prepared."""


class BotRouter:
    _FORBIDDEN = re.compile(
        r"\b(shell|terminal|command|python code|javascript code|change (?:your )?policy|"
        r"ignore (?:prior|previous)|another (?:telegram )?group|destination|"
        r"external (?:post|comment|action)|across every community|cross[- ]tawg|"
        r"execute .{0,30}(?:workflow|on-chain)|"
        r"deploy|sign transaction|private key|credential|model endpoint|write .{0,30} code|"
        r"执行命令|修改策略|换群|私钥|链上执行)\b",
        re.IGNORECASE,
    )
    _IDENTITY = re.compile(
        r"\b(identity correction|merge (?:my )?identity|same person|"
        r"my (?:handle|username)|I am @)\b|身份更正|身份纠正|这是我的账号",
        re.IGNORECASE,
    )
    _KNOWLEDGE_CORRECTION = re.compile(
        r"\b(correction|correct the|page is wrong|should say|old (?:rule|fact)|"
        r"update (?:the )?knowledge)\b|更正|纠正|知识库.{0,10}错|应该是",
        re.IGNORECASE,
    )
    _SOURCE = re.compile(
        r"\b(source suggestion|suggested source|add (?:this )?(?:source|link))\b|"
        r"资料建议|来源建议|这个链接|这个帖子|https?://",
        re.IGNORECASE,
    )
    _TAWG = re.compile(
        r"\b(TAWG|trustless[- ]ai|ERC[- ]?\d+|agent[- ]ercs|validation|settlement|"
        r"ethereum magicians|repository|repo|working group)\b",
        re.IGNORECASE,
    )
    _QUESTION = re.compile(
        r"[?\uFF1F]|\b(what|why|when|where|who|how|which|status|explain|summarize)\b",
        re.I,
    )

    def __init__(self, bot_username: str) -> None:
        self.bot_username = bot_username.casefold().lstrip("@")

    def classify(self, text: str) -> BotRoute:
        cleaned = re.sub(
            rf"@{re.escape(self.bot_username)}\b", "", text, flags=re.IGNORECASE
        ).strip()
        if self._FORBIDDEN.search(cleaned):
            return BotRoute.REFUSE
        if self._IDENTITY.search(cleaned):
            return BotRoute.IDENTITY_CORRECTION
        if self._KNOWLEDGE_CORRECTION.search(cleaned):
            return BotRoute.KNOWLEDGE_CORRECTION
        if self._SOURCE.search(cleaned):
            return BotRoute.SOURCE_SUGGESTION
        if self._TAWG.search(cleaned) and self._QUESTION.search(cleaned):
            return BotRoute.KNOWLEDGE_QUESTION
        return BotRoute.REFUSE


class ReplyAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["reply"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class _ReplyResult(StrictModel):
    schema_version: Literal["tawg.reply-result.v1"]
    reply_text: str
    language: str
    english_recap: str | None
    citations: list[str]
    correction_transaction: VaultTransaction | None
    refusal: bool


@dataclass(frozen=True, slots=True)
class PreparedReply:
    job_id: str
    reply_to_message_id: int
    reply_text: str
    citations: tuple[str, ...]
    language: str
    refusal: bool


class BotReplyService:
    def __init__(
        self,
        root: Path,
        *,
        ai: ReplyAi,
        bot_username: str,
        max_budget_usd: str = "1.00",
        timeout_seconds: float = 300,
    ) -> None:
        self.root = root.resolve()
        self.ai = ai
        self.router = BotRouter(bot_username)
        self.max_budget_usd = max_budget_usd
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply:
        self._require_utc(now)
        jobs = self._load_jobs()
        job = jobs.get(job_id)
        if job is None:
            raise ReplyRejected("unknown reply job")
        if job.status in {JobStatus.READY, JobStatus.DELIVERED}:
            return self._prepared(job)
        records = {record.record_id: record for record in SourceQuery(self.root).records()}
        trigger = records.get(job.trigger_record_id)
        if trigger is None:
            raise ReplyRejected("reply trigger evidence is missing")
        route = self.router.classify(trigger.text_original)
        if route is BotRoute.REFUSE:
            text = (
                "I can help with TAWG knowledge, local identity corrections, evidence-backed "
                "knowledge corrections, and relevant source suggestions. I can't take that action."
            )
            ready = job.model_copy(
                update={
                    "status": JobStatus.READY,
                    "prepared_reply_text": text,
                    "prepared_language": "en",
                    "prepared_citations": [],
                    "refusal": True,
                    "updated_at": now,
                    "safe_error_code": None,
                }
            )
            jobs[job_id] = ready
            self._publish_jobs(jobs, f"{job_id}:refused")
            return self._prepared(ready)

        processing = job.model_copy(
            update={
                "status": JobStatus.PROCESSING,
                "attempts": job.attempts + 1,
                "updated_at": now,
                "safe_error_code": None,
            }
        )
        jobs[job_id] = processing
        self._publish_jobs(jobs, f"{job_id}:processing")
        try:
            context = self._context(trigger, records, processing, route)
            raw = await self.ai.run(
                job_type="reply",
                context_pack=context,
                operation_id=job_id,
                max_budget_usd=self.max_budget_usd,
                timeout_seconds=self.timeout_seconds,
            )
            result = self._validate_result(raw, trigger, route, records, job_id)
            reply_text = result.reply_text.strip()
            if result.english_recap is not None:
                reply_text = f"{reply_text}\n\nEnglish recap: {result.english_recap.strip()}"
            if len(reply_text) > 8192:
                raise ReplyRejected("reply cannot fit in two Telegram messages")
            self.privacy.assert_public(reply_text)
            ready = processing.model_copy(
                update={
                    "status": JobStatus.READY,
                    "prepared_reply_text": reply_text,
                    "prepared_citations": result.citations,
                    "prepared_language": result.language,
                    "refusal": result.refusal,
                    "updated_at": now,
                }
            )
            jobs[job_id] = ready
            uow = RepositoryUnitOfWork(self.root, operation_id=job_id)
            if result.correction_transaction is not None:
                CorrectionService(VaultTransactionEngine(self.root)).stage(
                    result.correction_transaction,
                    operation_id=job_id,
                    uow=uow,
                )
            self._stage_jobs(uow, jobs)
            uow.publish()
            return self._prepared(ready)
        except Exception:
            failed_jobs = self._load_jobs()
            current = failed_jobs[job_id]
            failed_jobs[job_id] = current.model_copy(
                update={
                    "status": JobStatus.PENDING,
                    "updated_at": now,
                    "safe_error_code": "reply_prepare_failed",
                }
            )
            self._publish_jobs(failed_jobs, f"{job_id}:failed")
            raise ReplyRejected("reply preparation failed safely") from None

    def _context(
        self,
        trigger: SourceRecord,
        records: Mapping[str, SourceRecord],
        job: PendingBotJob,
        route: BotRoute,
    ) -> str:
        chain: list[SourceRecord] = []
        current = trigger
        seen = {trigger.record_id}
        while True:
            parent_ids = [
                relation.target_record_id
                for relation in current.relations
                if relation.relation_type == "reply_to"
            ]
            if not parent_ids or parent_ids[0] in seen:
                break
            parent = records.get(parent_ids[0])
            if parent is None:
                break
            chain.append(parent)
            seen.add(parent.record_id)
            current = parent
        chain.reverse()
        nearby = [
            record
            for record in records.values()
            if abs(record.created_at - trigger.created_at) <= timedelta(minutes=30)
            and record.record_id not in seen
        ]
        nearby.sort(key=lambda record: (record.created_at, record.record_id))
        retrieved = [
            {
                "chunk_id": item.chunk_id,
                "path": item.path,
                "text": item.text,
                "record_id": item.record_id,
                "source_locator": item.source_locator,
            }
            for item in VaultRetriever(self.root).query(trigger.text_original, top_k=16)
        ]
        inputs = ContextInputs(
            trigger={"route": route.value, "record": trigger.model_dump(mode="json")},
            reply_chain=[record.model_dump(mode="json") for record in chain],
            recent_telegram=[record.model_dump(mode="json") for record in nearby[:50]],
            retrieved=retrieved,
            citations=[
                {"record_id": record.record_id, "source_locator": record.source_locator}
                for record in sorted(
                    records.values(), key=lambda item: (item.created_at, item.record_id)
                )
            ],
            aliases=self._yaml_mapping(self.root / "knowledge/meta/aliases.yml"),
            job_state=job.model_dump(mode="json"),
            allowed_paths=(
                ["knowledge/"]
                if route in {BotRoute.IDENTITY_CORRECTION, BotRoute.KNOWLEDGE_CORRECTION}
                else []
            ),
            output_schema=self._json_mapping(
                self.root / "src/tawg_bot/schemas/reply-result.v1.json"
            ),
            budgets={"max_output_chars": 8000, "max_citations": 16},
        )
        try:
            return ContextPackBuilder(self.privacy).build(
                inputs, max_chars=250_000, max_recent_telegram=50
            ).text
        except ContextRejected as error:
            raise ReplyRejected(str(error)) from None

    def _validate_result(
        self,
        raw: Mapping[str, Any],
        trigger: SourceRecord,
        route: BotRoute,
        records: Mapping[str, SourceRecord],
        job_id: str,
    ) -> _ReplyResult:
        try:
            result = _ReplyResult.model_validate(raw)
        except ValidationError as error:
            raise ReplyRejected("invalid reply model output") from error
        if len(result.citations) != len(set(result.citations)):
            raise ReplyRejected("reply citations contain duplicates")
        if not set(result.citations).issubset(records):
            raise ReplyRejected("reply cites fabricated evidence")
        requester_non_english = bool(_NON_ENGLISH.search(trigger.text_original))
        if requester_non_english:
            if result.language.casefold().startswith("en") or not result.english_recap:
                raise ReplyRejected(
                    "non-English reply requires matching language and English recap"
                )
        elif not result.language.casefold().startswith("en") or result.english_recap is not None:
            raise ReplyRejected("English reply must not duplicate an English recap")
        correction_route = route in {
            BotRoute.IDENTITY_CORRECTION,
            BotRoute.KNOWLEDGE_CORRECTION,
        }
        if result.correction_transaction is not None and (
            not correction_route or result.correction_transaction.operation_id != job_id
        ):
            raise ReplyRejected("reply attempted an unauthorized correction")
        if route is BotRoute.KNOWLEDGE_QUESTION and not result.refusal and not result.citations:
            raise ReplyRejected("knowledge reply requires evidence citations")
        if result.refusal and result.correction_transaction is not None:
            raise ReplyRejected("refused reply cannot modify knowledge")
        try:
            self.privacy.assert_public(result.model_dump_json())
        except PrivacyViolation:
            raise ReplyRejected("reply output failed privacy validation") from None
        return result

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        path = self.root / "data/state/pending-bot-jobs.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            jobs = [PendingBotJob.model_validate(item) for item in raw]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ReplyRejected("invalid pending reply state") from error
        by_id = {job.job_id: job for job in jobs}
        if len(by_id) != len(jobs):
            raise ReplyRejected("duplicate pending reply job")
        return by_id

    def _publish_jobs(self, jobs: Mapping[str, PendingBotJob], operation_id: str) -> None:
        uow = RepositoryUnitOfWork(self.root, operation_id=operation_id)
        self._stage_jobs(uow, jobs)
        uow.publish()

    @staticmethod
    def _stage_jobs(
        uow: RepositoryUnitOfWork, jobs: Mapping[str, PendingBotJob]
    ) -> None:
        uow.stage_json(
            "data/state/pending-bot-jobs.json",
            [jobs[job_id].model_dump(mode="json") for job_id in sorted(jobs)],
        )

    @staticmethod
    def _prepared(job: PendingBotJob) -> PreparedReply:
        if job.prepared_reply_text is None or job.prepared_language is None:
            raise ReplyRejected("prepared reply state is incomplete")
        return PreparedReply(
            job_id=job.job_id,
            reply_to_message_id=job.reply_to_message_id,
            reply_text=job.prepared_reply_text,
            citations=tuple(job.prepared_citations),
            language=job.prepared_language,
            refusal=job.refusal,
        )

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("reply preparation time must use UTC")

    @staticmethod
    def _json_mapping(path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ReplyRejected("reply schema must be an object")
        return raw

    @staticmethod
    def _yaml_mapping(path: Path) -> dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ReplyRejected("alias registry must be a mapping")
        return raw
