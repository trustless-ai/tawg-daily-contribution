"""Permission-first routing and grounded preparation for Telegram mentions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol

import yaml
from pydantic import ValidationError

from tawg_bot.ai_router import AiRouteRejected, ContextualAiRouter
from tawg_bot.claude_cli import ClaudeCliError
from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.conversation_context import (
    ConversationContextBuilder,
    ConversationContextRejected,
)
from tawg_bot.corrections import CorrectionService
from tawg_bot.erc_query import ErcIntent, ErcQuery, ErcQueryPlanner
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.live_evidence import EvidencePack
from tawg_bot.models import (
    BotRoute,
    DeliveryAttempt,
    DeliveryStatus,
    JobStatus,
    PendingBotJob,
    Relation,
    SourceRecord,
    SourceType,
    StrictModel,
    TriggerKind,
)
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.source_registry import EvidenceKind
from tawg_bot.telegram_text import TelegramTextSplitError, split_telegram_text
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import parse_frontmatter
from tawg_bot.vault_transaction import CitationScope, VaultTransaction, VaultTransactionEngine

_NON_ENGLISH = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\u0400-\u052f\u0600-\u06ff\u0900-\u097f]")
_LIVE_ERC_REQUEST = re.compile(
    r"\b(latest|current|currently|today|recent|up[- ]to[- ]date|verify|recheck|"
    r"changed|updated|version|status)\b|最新|当前|现在|今天|近期|核实|验证|更新|版本|状态",
    re.IGNORECASE,
)
_INLINE_CITATION = re.compile(
    r"https?://|\[[^\]\s]+:[^\]]+\]|\[[^\]]+\]\(https?://[^)]+\)",
    re.IGNORECASE,
)
_LOCAL_CITATION = re.compile(r"\[((?:[A-Za-z0-9_.-]+:){2,}[A-Za-z0-9_.:/@-]+)\]")
_URL_CITATION = re.compile(r"https?://[^\s<>()\[\]]+", re.IGNORECASE)


class ReplyRejected(ValueError):
    """Raised when a mention or generated reply cannot safely be prepared."""

    def __init__(self, message: str, *, safe_code: str = "reply_prepare_failed") -> None:
        super().__init__(message)
        self.safe_code = safe_code


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
        r"update (?:the )?knowledge|add [^\n]{1,80} to (?:your|the) knowledge|"
        r"record [^\n]{1,80} (?:in|into) (?:your|the) knowledge)\b|"
        r"更正|纠正|知识库.{0,10}错|应该是",
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
    _RECENT_DISCUSSION_QUESTION = re.compile(
        r"^\s*(?:what (?:did we discuss|we discussed)(?: (?:just now|recently))?|"
        r"(?:please )?summari[sz]e (?:what we discussed|the (?:recent|latest) discussion))"
        r"\s*[?!.]*$",
        re.IGNORECASE,
    )
    _BOT_SOCIAL = re.compile(
        r"^\s*(?:(?:looks?|sounds?) good(?:[!,. ]+(?:you(?:'re| are|\u2019re) )?"
        r"(?:online|here|back|ready|present))?|(?:you(?:'re| are|\u2019re) )?"
        r"(?:online|here|back|ready|present)|(?:hi|hello|hey|thanks|thank you|welcome|"
        r"good (?:morning|afternoon|evening))(?:[!,. ]+(?:you(?:'re| are|\u2019re) )?"
        r"(?:online|here|back|ready|present))?)\s*[^\w]*$",
        re.IGNORECASE,
    )
    _MUTATION_AUTHORIZATION = re.compile(
        r"^\s*(?:(?:please|kindly)\s+)?(?:add|record|save|store|remember|correct|"
        r"update|create|suggest)\b|"
        r"^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:add|record|save|"
        r"store|remember|correct|update|create|suggest)\b|"
        r"^\s*(?:correction|source suggestion|suggested source)\b|"
        r"^\s*I\s+(?:suggest|recommend)\b|"
        r"\b(?:page|knowledge|fact|rule)\s+is\s+wrong\b|\bshould say\b|"
        r"^\s*(?:请)?(?:添加|记录|保存|记住|更正|纠正|更新|创建|建议)",
        re.IGNORECASE,
    )

    def __init__(self, bot_username: str) -> None:
        self.bot_username = bot_username.casefold().lstrip("@")
        self._erc_planner = ErcQueryPlanner()

    def classify(self, text: str) -> BotRoute:
        cleaned = self._clean(text)
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
        if self._RECENT_DISCUSSION_QUESTION.fullmatch(cleaned):
            return BotRoute.KNOWLEDGE_QUESTION
        if self._BOT_SOCIAL.fullmatch(cleaned):
            return BotRoute.COORDINATION
        return BotRoute.REFUSE

    def authorize_ai_route(
        self,
        text: str,
        route: BotRoute,
        trigger_kind: TriggerKind = TriggerKind.MENTION,
    ) -> BotRoute:
        """Clamp an AI decision to the controller's non-negotiable authority boundary."""
        if (
            trigger_kind is TriggerKind.GREETING_CANDIDATE
            and route is BotRoute.IGNORE
        ):
            return BotRoute.IGNORE
        if route is BotRoute.IGNORE:
            return BotRoute.REFUSE
        cleaned = self._clean(text)
        if self._FORBIDDEN.search(cleaned):
            return BotRoute.REFUSE
        if route is BotRoute.IDENTITY_CORRECTION and not self._IDENTITY.search(cleaned):
            return BotRoute.REFUSE
        if route in {
            BotRoute.KNOWLEDGE_CORRECTION,
            BotRoute.SOURCE_SUGGESTION,
        } and not self._MUTATION_AUTHORIZATION.search(cleaned):
            return BotRoute.REFUSE
        return route

    def erc_query(self, text: str) -> ErcQuery | None:
        if self.classify(text) not in {
            BotRoute.KNOWLEDGE_QUESTION,
            BotRoute.KNOWLEDGE_CORRECTION,
        }:
            return None
        return self._erc_planner.plan(text)

    def is_recent_discussion_question(self, text: str) -> bool:
        cleaned = self._clean(text)
        return self._RECENT_DISCUSSION_QUESTION.fullmatch(cleaned) is not None

    def _clean(self, text: str) -> str:
        return re.sub(
            rf"@{re.escape(self.bot_username)}\b", "", text, flags=re.IGNORECASE
        ).strip()


@dataclass(frozen=True, slots=True)
class _ReplyRepairSpec:
    trigger_id: str
    trigger_sha256: str
    refusal_sha256: str
    policy_version: str
    reason_code: str
    route: BotRoute
    recent_discussion: bool = False


class ReplyRepairReconciler:
    """Create auditable correction jobs for refusals invalidated by routing policy."""

    _STATE_PATH = "data/state/pending-bot-jobs.json"
    _LEGACY_REPAIRS: Mapping[str, _ReplyRepairSpec] = {
        "reply:tg:tawg:3380": _ReplyRepairSpec(
            trigger_id="tg:tawg:3380",
            trigger_sha256=(
                "dc6114743926cd5f4f9577807beb9211598fcff2c43b3244f2a1aa8a70660d5d"
            ),
            refusal_sha256=(
                "c88b75647067456eeb21dc284da1e93b36df61f1afc102ad6a913f19a6fde50e"
            ),
            policy_version="recent-discussion-v1",
            reason_code="recent_discussion_route_updated",
            route=BotRoute.KNOWLEDGE_QUESTION,
            recent_discussion=True,
        ),
        "reply:tg:tawg:3446": _ReplyRepairSpec(
            trigger_id="tg:tawg:3446",
            trigger_sha256=(
                "531b2cced7b3abfef0d043fe8a56fe6b4b4db8d2224946e56ab44d22d64700b9"
            ),
            refusal_sha256=(
                "c88b75647067456eeb21dc284da1e93b36df61f1afc102ad6a913f19a6fde50e"
            ),
            policy_version="knowledge-correction-v1",
            reason_code="knowledge_correction_route_updated",
            route=BotRoute.KNOWLEDGE_CORRECTION,
        ),
    }

    def __init__(self, root: Path, *, bot_username: str) -> None:
        self.root = root.resolve()
        self.router = BotRouter(bot_username)

    def reconcile(self, *, now: datetime) -> tuple[str, ...]:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("reply repair time must use UTC")
        jobs = self._load_jobs()
        records = {record.record_id: record for record in SourceQuery(self.root).records()}
        created: list[str] = []
        for original in sorted(jobs.values(), key=lambda item: item.job_id):
            if original.status is not JobStatus.DELIVERED or not original.refusal:
                continue
            repair_spec = self._LEGACY_REPAIRS.get(original.job_id)
            if repair_spec is None:
                continue
            trigger = records.get(repair_spec.trigger_id)
            prepared_text = original.prepared_reply_text
            if (
                original.trigger_record_id != repair_spec.trigger_id
                or trigger is None
                or trigger.content_sha256 != repair_spec.trigger_sha256
                or prepared_text is None
                or hashlib.sha256(prepared_text.encode("utf-8")).hexdigest()
                != repair_spec.refusal_sha256
                or self.router.classify(trigger.text_original) is not repair_spec.route
                or (
                    repair_spec.recent_discussion
                    and not self.router.is_recent_discussion_question(trigger.text_original)
                )
            ):
                continue
            repair_id = f"reply-repair:{repair_spec.policy_version}:{trigger.record_id}"
            if repair_id in jobs:
                continue
            jobs[repair_id] = PendingBotJob(
                job_id=repair_id,
                trigger_record_id=original.trigger_record_id,
                reply_to_message_id=original.reply_to_message_id,
                message_thread_id=original.message_thread_id,
                repair_of_job_id=original.job_id,
                repair_reason_code=repair_spec.reason_code,
                created_at=now,
                updated_at=now,
            )
            created.append(repair_id)
        if created:
            uow = RepositoryUnitOfWork(
                self.root,
                operation_id=f"reply-repair-reconcile:{int(now.timestamp())}",
            )
            uow.register_external_evidence(())
            uow.stage_json(
                self._STATE_PATH,
                [jobs[job_id].model_dump(mode="json") for job_id in sorted(jobs)],
            )
            uow.publish()
        return tuple(created)

    def _load_jobs(self) -> dict[str, PendingBotJob]:
        path = self.root / self._STATE_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            parsed = [PendingBotJob.model_validate(item) for item in raw]
        except (
            OSError,
            UnicodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            raise ReplyRejected("invalid pending reply state") from error
        jobs = {job.job_id: job for job in parsed}
        if len(jobs) != len(parsed):
            raise ReplyRejected("duplicate pending reply job")
        return jobs


class ReplyAi(Protocol):
    async def run(
        self,
        *,
        job_type: Literal["reply", "route"],
        context_pack: str,
        operation_id: str,
        max_budget_usd: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class LiveEvidenceProvider(Protocol):
    async def build(self, query: ErcQuery, *, now: datetime) -> EvidencePack: ...


class _ReplyResult(StrictModel):
    schema_version: Literal["tawg.reply-result.v2"]
    reply_text: str
    language: str
    english_recap: str | None
    citations: list[str]
    evidence_status: Literal["verified", "partial", "not_verified"]
    verification_gaps: list[str]
    correction_transaction: VaultTransaction | None
    refusal: bool


@dataclass(frozen=True, slots=True)
class PreparedReply:
    job_id: str
    reply_to_message_id: int
    message_thread_id: int | None
    reply_text: str
    citations: tuple[str, ...]
    language: str
    refusal: bool


@dataclass(frozen=True, slots=True)
class _ReplyContext:
    text: str
    allowed_citations: frozenset[str]
    evidence_pack: EvidencePack | None


@dataclass(frozen=True, slots=True)
class _LocalErcContext:
    citations: tuple[str, ...]
    pages: tuple[dict[str, str], ...]
    source_keys: tuple[str, ...]
    verified_at: tuple[str, ...]


class BotReplyService:
    _ROUTER_VERSION = "contextual-ai-v2"
    _ROUTE_TIMEOUT_SECONDS = 60.0
    _ROUTE_CONTEXT_MAX_CHARS = 64_000
    _ROUTE_CONTEXT_MAX_PRIOR_RECORDS = 100

    def __init__(
        self,
        root: Path,
        *,
        ai: ReplyAi,
        bot_username: str,
        live_evidence: LiveEvidenceProvider | None = None,
        knowledge_state: KnowledgeStateStore | None = None,
        chat_id: int | None = None,
        max_budget_usd: str = "1.00",
        route_max_budget_usd: str = "0.10",
        timeout_seconds: float = 300,
    ) -> None:
        self.root = root.resolve()
        self.ai = ai
        self.router = BotRouter(bot_username)
        self.live_evidence = live_evidence
        self.knowledge_state = knowledge_state
        if chat_id is not None and (
            isinstance(chat_id, bool) or not isinstance(chat_id, int)
        ):
            raise ValueError("configured Telegram chat ID must be an integer")
        self.chat_id = chat_id
        self.max_budget_usd = max_budget_usd
        self.route_max_budget_usd = self._bounded_route_budget(
            max_budget_usd,
            route_max_budget_usd,
        )
        self.timeout_seconds = timeout_seconds
        self.privacy = PrivacyFilter.from_yaml(self.root / "config/privacy.yml")

    async def prepare(self, job_id: str, *, now: datetime) -> PreparedReply | None:
        self._require_utc(now)
        model_deadline = monotonic() + self.timeout_seconds
        jobs = self._load_jobs()
        job = jobs.get(job_id)
        if job is None:
            raise ReplyRejected("unknown reply job")
        if job.status is JobStatus.IGNORED:
            return None
        if job.status in {JobStatus.READY, JobStatus.DELIVERED}:
            return self._prepared(job)
        records = {record.record_id: record for record in SourceQuery(self.root).records()}
        trigger = records.get(job.trigger_record_id)
        if trigger is None:
            raise ReplyRejected("reply trigger evidence is missing")
        records = self._with_audited_bot_parent(records, jobs, job, trigger)
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
        evidence_pack: EvidencePack | None = None
        failure_code = "reply_route_context_failed"
        try:
            route_context = ConversationContextBuilder(self.privacy).build(
                trigger=trigger,
                records=records.values(),
                message_thread_id=processing.message_thread_id,
                max_chars=self._ROUTE_CONTEXT_MAX_CHARS,
                max_prior_records=self._ROUTE_CONTEXT_MAX_PRIOR_RECORDS,
                trigger_kind=processing.trigger_kind,
            )
            routing_is_current = (
                processing.classified_route is not None
                and processing.router_context_sha256 == route_context.sha256
                and processing.router_version == self._ROUTER_VERSION
            )
            if not routing_is_current:
                if processing.classified_route is not None:
                    processing = processing.model_copy(
                        update={
                            "classified_route": None,
                            "router_context_sha256": None,
                            "router_version": None,
                            "routed_at": None,
                        }
                    )
                    jobs[job_id] = processing
                    self._publish_jobs(jobs, f"{job_id}:rerouting")
                failure_code = "reply_route_model_failed"
                decision = await ContextualAiRouter(self.ai).classify(
                    route_context,
                    operation_id=f"{job_id}:route",
                    max_budget_usd=self.route_max_budget_usd,
                    timeout_seconds=min(
                        self._ROUTE_TIMEOUT_SECONDS,
                        self._remaining_model_time(model_deadline),
                    ),
                )
                route = self.router.authorize_ai_route(
                    trigger.text_original,
                    decision.route,
                    processing.trigger_kind,
                )
                processing = processing.model_copy(
                    update={
                        "classified_route": route,
                        "router_context_sha256": decision.context_sha256,
                        "router_version": self._ROUTER_VERSION,
                        "routed_at": now,
                    }
                )
                jobs[job_id] = processing
                self._publish_jobs(jobs, f"{job_id}:routed")
            else:
                assert processing.classified_route is not None
                route = processing.classified_route

            if route is BotRoute.IGNORE:
                ignored = processing.model_copy(
                    update={
                        "status": JobStatus.IGNORED,
                        "prepared_reply_text": None,
                        "prepared_language": None,
                        "prepared_citations": [],
                        "refusal": False,
                        "updated_at": now,
                        "safe_error_code": None,
                    }
                )
                jobs[job_id] = ignored
                self._publish_jobs(jobs, f"{job_id}:ignored")
                return None

            if route is BotRoute.REFUSE:
                text = (
                    "I can help with TAWG knowledge, local identity corrections, evidence-backed "
                    "knowledge corrections, and relevant source suggestions. I can't take that "
                    "action."
                )
                ready = processing.model_copy(
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

            failure_code = "reply_context_failed"
            erc_query = self.router.erc_query(trigger.text_original)
            local_erc_context: _LocalErcContext | None = None
            if erc_query is not None:
                local_erc_context = self._local_erc_context(
                    erc_query,
                    include_revision=route is BotRoute.KNOWLEDGE_CORRECTION,
                )
                local_erc_citations = (
                    local_erc_context.citations if local_erc_context is not None else ()
                )
                if self._needs_live_erc_evidence(
                    trigger.text_original,
                    erc_query,
                    local_erc_citations,
                ):
                    if self.live_evidence is None or self.knowledge_state is None:
                        raise ReplyRejected("live ERC evidence is not configured")
                    failure_code = "reply_evidence_failed"
                    evidence_pack = await self.live_evidence.build(erc_query, now=now)
                    failure_code = "reply_context_failed"
            context = self._context(
                trigger,
                records,
                processing,
                route,
                evidence_pack=evidence_pack,
                local_erc_context=local_erc_context,
            )
            failure_code = "reply_model_failed"
            raw = await self.ai.run(
                job_type="reply",
                context_pack=context.text,
                operation_id=job_id,
                max_budget_usd=self.max_budget_usd,
                timeout_seconds=self._remaining_model_time(model_deadline),
            )
            failure_code = "reply_validation_failed"
            result = self._validate_result(
                raw,
                trigger,
                route,
                context.allowed_citations,
                context.evidence_pack,
                job_id,
                correction_source_keys=(
                    frozenset(local_erc_context.source_keys)
                    if local_erc_context is not None
                    else frozenset()
                ),
            )
            reply_text = result.reply_text.strip()
            if result.english_recap is not None:
                reply_text = f"{reply_text}\n\nEnglish recap: {result.english_recap.strip()}"
            failure_code = "reply_citation_binding_failed"
            reply_text = self._bind_reply_citations(reply_text, result.citations)
            if len(reply_text) > 8192:
                raise ReplyRejected("reply cannot fit in two Telegram messages")
            failure_code = "reply_privacy_failed"
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
            failure_code = "reply_persistence_failed"
            uow = RepositoryUnitOfWork(self.root, operation_id=job_id)
            external_texts = (
                tuple(item.text for item in evidence_pack.evidence)
                if evidence_pack is not None
                else ()
            )
            uow.register_external_evidence(external_texts)
            if evidence_pack is not None:
                assert self.knowledge_state is not None
                self.knowledge_state.stage_evidence_outcome(
                    uow, evidence_pack.for_persistence(), now=now
                )
            elif route is BotRoute.SOURCE_SUGGESTION:
                if self.knowledge_state is None:
                    raise ReplyRejected("source candidate state is not configured")
                urls = self._suggested_urls(trigger.text_original)
                if not urls:
                    raise ReplyRejected("source suggestion has no safe URL")
                self.knowledge_state.add_candidates(uow, urls, trigger.record_id, now)
            if result.correction_transaction is not None:
                citation_scope = (
                    CitationScope(
                        source_keys=frozenset(local_erc_context.source_keys),
                        urls=frozenset(local_erc_context.citations),
                    )
                    if local_erc_context is not None
                    else None
                )
                CorrectionService(
                    VaultTransactionEngine(self.root, citation_scope=citation_scope)
                ).stage(
                    result.correction_transaction,
                    operation_id=job_id,
                    uow=uow,
                )
            self._stage_jobs(uow, jobs)
            uow.publish()
            return self._prepared(ready)
        except Exception as error:
            safe_error_code = self._safe_failure_code(failure_code, error)
            failed_jobs = self._load_jobs()
            current = failed_jobs[job_id]
            failed_jobs[job_id] = current.model_copy(
                update={
                    "status": JobStatus.PENDING,
                    "updated_at": now,
                    "safe_error_code": safe_error_code,
                }
            )
            failure_uow = RepositoryUnitOfWork(self.root, operation_id=f"{job_id}:failed")
            if evidence_pack is not None:
                failure_uow.register_external_evidence(item.text for item in evidence_pack.evidence)
            else:
                failure_uow.register_external_evidence(())
            if evidence_pack is not None and self.knowledge_state is not None:
                self.knowledge_state.stage_evidence_outcome(
                    failure_uow, evidence_pack.for_persistence(), now=now
                )
            self._stage_jobs(failure_uow, failed_jobs)
            failure_uow.publish()
            raise ReplyRejected(
                "reply preparation failed safely", safe_code=safe_error_code
            ) from None

    @staticmethod
    def _safe_failure_code(stage_code: str, error: Exception) -> str:
        if stage_code == "reply_route_context_failed" and isinstance(
            error, ConversationContextRejected
        ):
            return "reply_route_context_invalid"
        if stage_code == "reply_route_model_failed":
            if isinstance(error, AiRouteRejected):
                return "reply_route_model_schema_invalid"
            if isinstance(error, ClaudeCliError):
                message = str(error)
                if message == "Claude Code exceeded its time limit":
                    return "reply_route_model_timeout"
                if message == "Claude Code structured output failed schema validation":
                    return "reply_route_model_schema_invalid"
                if message == "Claude Code could not be started" or message.startswith(
                    "Claude Code failed with exit status"
                ):
                    return "reply_route_model_process_failed"
                return "reply_route_model_contract_invalid"
            return stage_code
        if stage_code == "reply_validation_failed" and isinstance(error, ReplyRejected):
            return {
                "invalid reply model output": "reply_model_output_invalid",
                "reply citations contain duplicates": "reply_citations_duplicate",
                "reply cites fabricated evidence": "reply_citation_not_allowed",
                "reply text repeats an evidence citation": "reply_text_citation_duplicate",
                "reply text cites undeclared evidence": "reply_text_citation_undeclared",
                "reply overstates missing normative evidence": "reply_evidence_status_invalid",
                "reply overstates incomplete evidence": "reply_evidence_status_invalid",
                "reply hides required evidence gaps": "reply_evidence_gaps_missing",
                "non-English reply requires matching language and English recap": (
                    "reply_language_invalid"
                ),
                "English reply must not duplicate an English recap": "reply_language_invalid",
                "reply attempted an unauthorized correction": "reply_correction_unauthorized",
                "knowledge reply requires evidence citations": "reply_knowledge_citation_missing",
                "coordination reply cannot cite evidence": (
                    "reply_coordination_citation_forbidden"
                ),
                "refused reply cannot modify knowledge": "reply_correction_unauthorized",
                "reply output failed privacy validation": "reply_output_privacy_failed",
            }.get(str(error), stage_code)
        if stage_code != "reply_model_failed" or not isinstance(error, ClaudeCliError):
            return stage_code
        message = str(error)
        if message == "Claude Code exceeded its time limit":
            return "reply_model_timeout"
        if message == "Claude Code could not be started" or message.startswith(
            "Claude Code failed with exit status"
        ):
            return "reply_model_process_failed"
        if message == "Claude Code structured output failed schema validation":
            return "reply_model_schema_invalid"
        if message in {
            "Claude Code did not return one bounded successful result",
            "Claude Code returned no structured output",
            "Claude Code returned invalid JSON",
        }:
            return "reply_model_contract_invalid"
        return stage_code

    @staticmethod
    def _remaining_model_time(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ClaudeCliError("Claude Code exceeded its time limit")
        return remaining

    @staticmethod
    def _bounded_route_budget(overall: str, route: str) -> str:
        try:
            overall_budget = Decimal(overall)
            route_budget = Decimal(route)
        except InvalidOperation:
            raise ValueError("model budgets must be decimals") from None
        if (
            not overall_budget.is_finite()
            or not route_budget.is_finite()
            or overall_budget <= 0
            or route_budget <= 0
        ):
            raise ValueError("model budgets must be positive finite decimals")
        return format(min(overall_budget, route_budget), "f")

    def _context(
        self,
        trigger: SourceRecord,
        records: Mapping[str, SourceRecord],
        job: PendingBotJob,
        route: BotRoute,
        *,
        evidence_pack: EvidencePack | None,
        local_erc_context: _LocalErcContext | None = None,
    ) -> _ReplyContext:
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
        recent_discussion = self.router.is_recent_discussion_question(
            trigger.text_original
        )
        if recent_discussion:
            nearby = [
                record for record in nearby if record.created_at <= trigger.created_at
            ]
        nearby.sort(key=lambda record: (record.created_at, record.record_id))
        retrieved_items = (
            []
            if recent_discussion
            else VaultRetriever(self.root).query(trigger.text_original, top_k=16)
        )
        retrieved: list[dict[str, Any]] = (
            list(local_erc_context.pages)
            if local_erc_context is not None and not recent_discussion
            else []
        )
        local_erc_paths = (
            {page["path"] for page in local_erc_context.pages}
            if local_erc_context is not None
            else set()
        )
        retrieved.extend(
            [
                {
                    "chunk_id": item.chunk_id,
                    "path": item.path,
                    "text": item.text,
                    "record_id": item.record_id,
                    "source_locator": item.source_locator,
                }
                for item in retrieved_items
                if item.path not in local_erc_paths
            ]
        )
        local_ids: set[str] = (
            seen
            | {record.record_id for record in nearby[:50]}
            | {item.record_id for item in retrieved_items if item.record_id is not None}
        )
        local_ids -= {
            record.record_id
            for record in records.values()
            if record.source_payload.get("message_kind") == "audited_bot_delivery"
        }
        if recent_discussion:
            local_ids.discard(trigger.record_id)
            local_ids = {
                record_id
                for record_id in local_ids
                if record_id in records
                and records[record_id].created_at <= trigger.created_at
            }
        allowed_citations: frozenset[str]
        citation_entries: list[dict[str, str]]
        if route is BotRoute.COORDINATION:
            allowed_citations = frozenset()
            citation_entries = []
        elif evidence_pack is not None:
            allowed_citations = frozenset(evidence_pack.citation_allowlist)
            citation_entries = [{"url": url} for url in evidence_pack.citation_allowlist]
        else:
            local_erc_citations = (
                local_erc_context.citations if local_erc_context is not None else ()
            )
            allowed_citations = frozenset(local_ids | set(local_erc_citations))
            citation_entries = [
                {
                    "record_id": record.record_id,
                    "source_locator": record.source_locator,
                }
                for record in sorted(
                    (records[record_id] for record_id in local_ids),
                    key=lambda item: (item.created_at, item.record_id),
                )
            ]
            citation_entries.extend({"url": url} for url in local_erc_citations)
        trigger_context: dict[str, Any] = {
            "route": route.value,
            "record": trigger.model_dump(mode="json"),
        }
        if evidence_pack is not None:
            trigger_context["erc_evidence_mode"] = "live"
        elif local_erc_context is not None:
            trigger_context["erc_evidence_mode"] = "local_synthesis"
            trigger_context["local_verified_at"] = list(local_erc_context.verified_at)
        inputs = ContextInputs(
            trigger=trigger_context,
            reply_chain=[record.model_dump(mode="json") for record in chain],
            recent_telegram=[record.model_dump(mode="json") for record in nearby[:50]],
            retrieved=retrieved,
            citations=citation_entries,
            aliases=self._yaml_mapping(self.root / "knowledge/meta/aliases.yml"),
            job_state=job.model_dump(mode="json"),
            allowed_paths=(
                ["knowledge/"]
                if route in {BotRoute.IDENTITY_CORRECTION, BotRoute.KNOWLEDGE_CORRECTION}
                else []
            ),
            output_schema=self._json_mapping(
                self.root / "src/tawg_bot/schemas/reply-result.v2.json"
            ),
            budgets={"max_output_chars": 8000, "max_citations": 16},
            evidence_pack=(
                evidence_pack.model_dump(mode="json") if evidence_pack is not None else None
            ),
            citation_allowlist=list(allowed_citations),
        )
        try:
            packed = ContextPackBuilder(self.privacy).build(
                inputs, max_chars=250_000, max_recent_telegram=50
            )
            return _ReplyContext(
                text=packed.text,
                allowed_citations=allowed_citations,
                evidence_pack=evidence_pack,
            )
        except ContextRejected as error:
            raise ReplyRejected(str(error)) from None

    def _with_audited_bot_parent(
        self,
        records: Mapping[str, SourceRecord],
        jobs: Mapping[str, PendingBotJob],
        job: PendingBotJob,
        trigger: SourceRecord,
    ) -> dict[str, SourceRecord]:
        augmented = dict(records)
        if job.trigger_kind is not TriggerKind.REPLY_TO_BOT:
            return augmented
        if self.chat_id is None:
            raise ReplyRejected("direct reply preparation requires configured chat identity")
        reply_targets = [
            relation.target_record_id
            for relation in trigger.relations
            if relation.relation_type == "reply_to"
        ]
        if len(reply_targets) != 1:
            return augmented
        target_record_id = reply_targets[0]
        augmented.pop(target_record_id, None)
        target_message_id = target_record_id.rsplit(":", 1)[-1]
        if not target_message_id.isdigit():
            raise ReplyRejected("direct reply target is invalid")
        attempts = self._load_delivery_attempts()
        matching = [
            attempt
            for attempt in attempts
            if attempt.status is DeliveryStatus.DELIVERED
            and attempt.telegram_chat_id == self.chat_id
            and int(target_message_id) in attempt.telegram_message_ids
        ]
        if len(matching) != 1:
            raise ReplyRejected("direct reply target lacks one audited bot delivery")
        attempt = matching[0]
        if attempt.message_thread_id != job.message_thread_id:
            raise ReplyRejected("direct reply target failed thread audit binding")
        delivered_text = self._audited_delivery_text(attempt, jobs)
        if delivered_text is None:
            return augmented
        try:
            delivered_messages = split_telegram_text(
                delivered_text,
                limit=32_768,
                max_messages=2,
            )
        except TelegramTextSplitError as error:
            raise ReplyRejected("audited bot delivery text cannot be reconstructed") from error
        if len(delivered_messages) != len(attempt.telegram_message_ids):
            raise ReplyRejected("audited bot delivery message count does not match")
        target_index = attempt.telegram_message_ids.index(int(target_message_id))
        target_text = delivered_messages[target_index]
        group_parts = target_record_id.split(":", 2)
        if len(group_parts) != 3 or group_parts[0] != "tg" or not group_parts[1]:
            raise ReplyRejected("direct reply target is outside Telegram scope")
        relations = (
            [
                Relation(
                    relation_type="reply_to",
                    target_record_id=f"tg:{group_parts[1]}:{attempt.reply_to_message_id}",
                )
            ]
            if attempt.reply_to_message_id is not None
            else []
        )
        delivered_at = attempt.sent_at or attempt.updated_at
        if delivered_at >= trigger.created_at:
            raise ReplyRejected("direct reply target does not precede its trigger")
        augmented[target_record_id] = SourceRecord.from_text(
            record_id=target_record_id,
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator=(
                "repo:data/state/delivery-state.json#"
                f"{attempt.delivery_id}:{target_message_id}"
            ),
            author_person_id="tawg-bot",
            author_source_handle=f"@{self.router.bot_username}",
            created_at=delivered_at,
            updated_at=delivered_at,
            text_original=target_text,
            ingested_at=attempt.updated_at,
            relations=relations,
            source_payload={
                "message_kind": "audited_bot_delivery",
                "message_thread_id": attempt.message_thread_id,
            },
        )
        return augmented

    def _audited_delivery_text(
        self,
        attempt: DeliveryAttempt,
        jobs: Mapping[str, PendingBotJob],
    ) -> str | None:
        delivered_job = jobs.get(attempt.job_id)
        if delivered_job is not None:
            delivered_text = delivered_job.prepared_reply_text
            if (
                delivered_job.status is not JobStatus.DELIVERED
                or delivered_text is None
                or attempt.content_sha256
                != hashlib.sha256(delivered_text.encode("utf-8")).hexdigest()
            ):
                raise ReplyRejected("direct reply target failed delivery audit binding")
            return delivered_text
        if not attempt.job_id.startswith("daily:"):
            return None
        path = self.root / "data/state/prepared-daily.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReplyRejected("invalid prepared Daily audit state") from error
        if not isinstance(raw, dict) or raw.get("window_id") != attempt.job_id:
            return None
        delivered_text = raw.get("telegram_text")
        if not isinstance(delivered_text, str) or not delivered_text.strip():
            raise ReplyRejected("prepared Daily audit text is invalid")
        if attempt.content_sha256 != hashlib.sha256(
            delivered_text.encode("utf-8")
        ).hexdigest():
            raise ReplyRejected("prepared Daily text failed delivery audit binding")
        return delivered_text

    def _load_delivery_attempts(self) -> tuple[DeliveryAttempt, ...]:
        path = self.root / "data/state/delivery-state.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            attempts = tuple(DeliveryAttempt.model_validate(item) for item in raw)
        except (
            OSError,
            UnicodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            raise ReplyRejected("invalid delivery state for direct reply") from error
        delivery_ids = {attempt.delivery_id for attempt in attempts}
        if len(delivery_ids) != len(attempts):
            raise ReplyRejected("duplicate delivery audit for direct reply")
        return attempts

    def _local_erc_context(
        self,
        query: ErcQuery,
        *,
        include_revision: bool = False,
    ) -> _LocalErcContext | None:
        if self.knowledge_state is None:
            return None
        citations: list[str] = []
        pages: list[dict[str, str]] = []
        context_source_keys: list[str] = []
        verified_times: list[str] = []
        for erc_number in query.erc_numbers:
            page = self.root / f"knowledge/ercs/erc-{erc_number}.md"
            if not page.is_file() or page.is_symlink():
                return None
            try:
                current_bytes = page.read_bytes()
                current = current_bytes.decode("utf-8")
                frontmatter, body = parse_frontmatter(current)
            except (OSError, UnicodeError, ValueError):
                return None
            source_keys = frontmatter.get("source_keys") if frontmatter else None
            verified_at = frontmatter.get("verified_at") if frontmatter else None
            if not isinstance(source_keys, list) or not all(
                isinstance(source_key, str) for source_key in source_keys
            ) or not isinstance(verified_at, str | datetime):
                return None
            active = {
                source.source_key: source
                for source in self.knowledge_state.registry.resolve(
                    erc_number,
                    frozenset(EvidenceKind),
                )
            }
            page_citations = [
                active[source_key].canonical_url
                for source_key in source_keys
                if source_key in active
            ]
            if not page_citations:
                return None
            citations.extend(page_citations)
            context_source_keys.extend(
                source_key for source_key in source_keys if source_key in active
            )
            relative = page.relative_to(self.root).as_posix()
            context_page = {
                "chunk_id": f"local:erc-{erc_number}",
                "path": relative,
                "text": current if include_revision else body[:20_000],
                "record_id": "",
                "source_locator": "",
            }
            if include_revision:
                context_page["expected_sha256"] = hashlib.sha256(current_bytes).hexdigest()
            pages.append(context_page)
            verified_times.append(
                verified_at.isoformat().replace("+00:00", "Z")
                if isinstance(verified_at, datetime)
                else verified_at
            )
        return _LocalErcContext(
            citations=tuple(dict.fromkeys(citations)),
            pages=tuple(pages),
            source_keys=tuple(dict.fromkeys(context_source_keys)),
            verified_at=tuple(verified_times),
        )

    @staticmethod
    def _needs_live_erc_evidence(
        text: str,
        query: ErcQuery,
        local_citations: tuple[str, ...],
    ) -> bool:
        return (
            not local_citations
            or query.intent is ErcIntent.STATUS
            or _LIVE_ERC_REQUEST.search(text) is not None
        )

    def _validate_result(
        self,
        raw: Mapping[str, Any],
        trigger: SourceRecord,
        route: BotRoute,
        allowed_citations: frozenset[str],
        evidence_pack: EvidencePack | None,
        job_id: str,
        correction_source_keys: frozenset[str],
    ) -> _ReplyResult:
        try:
            result = _ReplyResult.model_validate(raw)
        except ValidationError as error:
            raise ReplyRejected("invalid reply model output") from error
        requester_non_english = bool(_NON_ENGLISH.search(trigger.text_original))
        if (
            not requester_non_english
            and result.language.casefold().startswith("en")
            and result.english_recap
        ):
            result = result.model_copy(
                update={
                    "reply_text": (
                        f"{result.reply_text.rstrip()}\n\n{result.english_recap.strip()}"
                    ),
                    "english_recap": None,
                }
            )
        normalized_reply_text = self._normalize_local_citation_rendering(
            result.reply_text, allowed_citations
        )
        normalized_english_recap = (
            self._normalize_local_citation_rendering(
                result.english_recap, allowed_citations
            )
            if result.english_recap is not None
            else None
        )
        if (
            normalized_reply_text != result.reply_text
            or normalized_english_recap != result.english_recap
        ):
            result = result.model_copy(
                update={
                    "reply_text": normalized_reply_text,
                    "english_recap": normalized_english_recap,
                }
            )
        if len(result.citations) != len(set(result.citations)):
            raise ReplyRejected("reply citations contain duplicates")
        if not set(result.citations).issubset(allowed_citations):
            raise ReplyRejected("reply cites fabricated evidence")
        deduplicated_reply_text, deduplicated_english_recap = (
            self._deduplicate_declared_local_citations(
                result.reply_text,
                result.english_recap,
                frozenset(result.citations),
            )
        )
        if (
            deduplicated_reply_text != result.reply_text
            or deduplicated_english_recap != result.english_recap
        ):
            result = result.model_copy(
                update={
                    "reply_text": deduplicated_reply_text,
                    "english_recap": deduplicated_english_recap,
                }
            )
        model_text = result.reply_text
        if result.english_recap is not None:
            model_text = f"{model_text}\n{result.english_recap}"
        text_citations = self._text_citations(model_text)
        if len(text_citations) != len(set(text_citations)):
            raise ReplyRejected("reply text repeats an evidence citation")
        if not set(text_citations).issubset(result.citations):
            raise ReplyRejected("reply text cites undeclared evidence")
        if evidence_pack is not None and evidence_pack.missing_required:
            normative_missing = any(
                missing.bucket == "normative_spec" for missing in evidence_pack.missing_required
            )
            if normative_missing and result.evidence_status != "not_verified":
                raise ReplyRejected("reply overstates missing normative evidence")
            if not normative_missing and result.evidence_status == "verified":
                raise ReplyRejected("reply overstates incomplete evidence")
            if not result.verification_gaps:
                raise ReplyRejected("reply hides required evidence gaps")
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
        if result.correction_transaction is not None:
            allowed_transaction_citations = allowed_citations | correction_source_keys
            if any(
                not set(write.citations).issubset(allowed_transaction_citations)
                for write in result.correction_transaction.writes
            ):
                raise ReplyRejected("correction cites evidence outside its context")
        if route is BotRoute.KNOWLEDGE_QUESTION and not result.refusal and not result.citations:
            raise ReplyRejected("knowledge reply requires evidence citations")
        if route is BotRoute.COORDINATION and (
            result.citations or _INLINE_CITATION.search(result.reply_text)
        ):
            raise ReplyRejected("coordination reply cannot cite evidence")
        if result.refusal and result.correction_transaction is not None:
            raise ReplyRejected("refused reply cannot modify knowledge")
        try:
            self.privacy.assert_public(result.model_dump_json())
        except PrivacyViolation:
            raise ReplyRejected("reply output failed privacy validation") from None
        return result

    @classmethod
    def _bind_reply_citations(cls, text: str, citations: list[str]) -> str:
        occurrences = cls._text_citations(text)
        if len(occurrences) != len(set(occurrences)):
            raise ReplyRejected("reply text repeats an evidence citation")
        present = set(occurrences)
        missing = [citation for citation in citations if citation not in present]
        if missing:
            rendered = [
                f"[{citation}]" if ":" in citation and not citation.startswith("http") else citation
                for citation in missing
            ]
            text = f"{text}\n\nSources:\n" + "\n".join(f"• {item}" for item in rendered)
        bound = cls._text_citations(text)
        if len(bound) != len(citations) or set(bound) != set(citations):
            raise ReplyRejected("reply text citations do not match declared evidence")
        return text

    @staticmethod
    def _text_citations(text: str) -> tuple[str, ...]:
        found: list[str] = _LOCAL_CITATION.findall(text)
        found.extend(
            match.rstrip(".,;:!?") for match in _URL_CITATION.findall(text)
        )
        return tuple(found)

    @staticmethod
    def _normalize_local_citation_rendering(
        text: str, allowed_citations: frozenset[str]
    ) -> str:
        normalized = text
        for citation in sorted(allowed_citations, key=lambda value: (-len(value), value)):
            if ":" not in citation or citation.startswith("http"):
                continue
            normalized = normalized.replace(
                f"[record:{citation}]",
                f"[{citation}]",
            )
        return normalized

    @staticmethod
    def _deduplicate_declared_local_citations(
        reply_text: str,
        english_recap: str | None,
        declared_citations: frozenset[str],
    ) -> tuple[str, str | None]:
        seen: set[str] = set()

        def deduplicate(text: str) -> str:
            def replace(match: re.Match[str]) -> str:
                citation = match.group(1)
                if citation not in declared_citations:
                    return match.group(0)
                if citation in seen:
                    return ""
                seen.add(citation)
                return match.group(0)

            return _LOCAL_CITATION.sub(replace, text)

        return (
            deduplicate(reply_text),
            deduplicate(english_recap) if english_recap is not None else None,
        )

    @staticmethod
    def _suggested_urls(text: str) -> tuple[str, ...]:
        matches = re.findall(r"https?://[^\s<>()]+", text, flags=re.IGNORECASE)
        cleaned = [value.rstrip('.,;:!?)"]}') for value in matches]
        return tuple(dict.fromkeys(cleaned[:4]))

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
        uow.register_external_evidence(())
        self._stage_jobs(uow, jobs)
        uow.publish()

    @staticmethod
    def _stage_jobs(uow: RepositoryUnitOfWork, jobs: Mapping[str, PendingBotJob]) -> None:
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
            message_thread_id=job.message_thread_id,
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
