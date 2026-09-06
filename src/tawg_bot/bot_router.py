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
from typing import Any, Literal, Protocol, cast

import yaml
from pydantic import Field, ValidationError, field_validator

from tawg_bot.ai_router import AiRouteRejected, ContextualAiRouter
from tawg_bot.aliases import AliasRegistry
from tawg_bot.claude_cli import ClaudeCliError
from tawg_bot.context import ContextInputs, ContextPackBuilder, ContextRejected
from tawg_bot.conversation_context import (
    ConversationContextBuilder,
    ConversationContextRejected,
)
from tawg_bot.corrections import CorrectionService
from tawg_bot.erc_query import ErcIntent, ErcQuery, ErcQueryPlanner
from tawg_bot.github_announcements import (
    GitHubAnnouncementClient,
    GitHubAnnouncementRejected,
    resolve_referenced_pull_evidence,
)
from tawg_bot.http import SafeJsonHttpClient
from tawg_bot.invinoveritas_verify import (
    VerificationRejected,
    build_verification_proof_attachment,
    format_verification_reply,
    verify_and_confirm,
)
from tawg_bot.knowledge_jobs import KnowledgeStateStore
from tawg_bot.knowledge_mutation import (
    KnowledgeMutationCapability,
    KnowledgeMutationRejected,
    build_mutation_capability,
    canonicalize_new_knowledge_transaction,
    extract_public_https_urls,
    validate_knowledge_transaction,
)
from tawg_bot.live_evidence import EvidencePack
from tawg_bot.member_welcome import (
    build_member_ai_context,
    build_member_welcome_reply,
    member_profile_snapshot,
    member_welcome_is_expired,
)
from tawg_bot.models import (
    BotRoute,
    DeliveryAttempt,
    DeliveryStatus,
    JobStatus,
    PendingBotJob,
    PreparedAttachment,
    Relation,
    RouteContextScope,
    SourceRecord,
    SourceType,
    StrictModel,
    TriggerKind,
)
from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.privacy import PrivacyFilter, PrivacyViolation
from tawg_bot.query import SourceQuery
from tawg_bot.retrieval import VaultRetriever
from tawg_bot.scan_targets import (
    ScanRegistrationProposal,
    ScanTargetRegistry,
    ScanTargetRejected,
    ScanTargetStore,
    ScanTargetVerifier,
    normalize_magicians_topic_url,
)
from tawg_bot.source_registry import EvidenceKind
from tawg_bot.telegram_intake import resolve_member_welcome_target
from tawg_bot.telegram_text import TelegramTextSplitError, split_telegram_text
from tawg_bot.unit_of_work import RepositoryUnitOfWork, safe_operation_id
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
_TELEGRAM_MENTION = re.compile(r"(?<![A-Za-z0-9_@])@[A-Za-z0-9_]{1,64}(?![A-Za-z0-9_])")
_UNSAFE_MEMBER_LOCATOR = re.compile(
    r"(?:\b(?:www\.|t\.me/|telegram\.me/)|"
    r"\[[^\]]+\]\([^)]+\)|"
    r"\b(?:mailto|ftp|tg):|"
    r"(?<![@\w.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/\S*)?)",
    re.IGNORECASE,
)
_URL_CITATION = re.compile(r"https?://[^\s<>()\[\]]+", re.IGNORECASE)
_RENDERED_URL_CITATION = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<link>https?://[^\s<>()\[\]]+)\)"
    r"|<(?P<autolink>https?://[^\s<>()\[\]]+)>"
    r"|(?P<bare>https?://[^\s<>()\[\]]+)",
    re.IGNORECASE,
)


class ReplyRejected(ValueError):
    """Raised when a mention or generated reply cannot safely be prepared."""

    def __init__(self, message: str, *, safe_code: str = "reply_prepare_failed") -> None:
        super().__init__(message)
        self.safe_code = safe_code


class BotRouter:
    def __init__(self, bot_username: str) -> None:
        self.bot_username = bot_username.casefold().lstrip("@")
        self._erc_planner = ErcQueryPlanner()

    def authorize_ai_route(
        self,
        route: BotRoute,
        trigger_kind: TriggerKind = TriggerKind.MENTION,
    ) -> BotRoute:
        """Clamp an AI decision to the controller's non-negotiable authority boundary."""
        if trigger_kind is TriggerKind.GREETING_CANDIDATE and route is BotRoute.IGNORE:
            return BotRoute.IGNORE
        if route is BotRoute.IGNORE:
            return BotRoute.REFUSE
        return route

    def erc_query(self, text: str) -> ErcQuery | None:
        return self._erc_planner.plan(text)


@dataclass(frozen=True, slots=True)
class _ReplyRepairSpec:
    trigger_id: str
    trigger_sha256: str
    prepared_text_sha256: str
    policy_version: str
    reason_code: str
    requires_refusal: bool = True


class ReplyRepairReconciler:
    """Create auditable correction jobs for exact invalidated delivered replies."""

    _STATE_PATH = "data/state/pending-bot-jobs.json"
    _LEGACY_REPAIRS: Mapping[str, _ReplyRepairSpec] = {
        "reply:tg:tawg:3380": _ReplyRepairSpec(
            trigger_id="tg:tawg:3380",
            trigger_sha256=("dc6114743926cd5f4f9577807beb9211598fcff2c43b3244f2a1aa8a70660d5d"),
            prepared_text_sha256=(
                "c88b75647067456eeb21dc284da1e93b36df61f1afc102ad6a913f19a6fde50e"
            ),
            policy_version="recent-discussion-v1",
            reason_code="recent_discussion_route_updated",
        ),
        "reply:tg:tawg:3446": _ReplyRepairSpec(
            trigger_id="tg:tawg:3446",
            trigger_sha256=("531b2cced7b3abfef0d043fe8a56fe6b4b4db8d2224946e56ab44d22d64700b9"),
            prepared_text_sha256=(
                "c88b75647067456eeb21dc284da1e93b36df61f1afc102ad6a913f19a6fde50e"
            ),
            policy_version="knowledge-correction-v1",
            reason_code="knowledge_correction_route_updated",
        ),
        "reply:tg:tawg:3470": _ReplyRepairSpec(
            trigger_id="tg:tawg:3470",
            trigger_sha256=("2110c0c18a6ce873a957d9b18cbf577b85fe7c2d2bc18cfe874db14231c06e70"),
            prepared_text_sha256=(
                "c88b75647067456eeb21dc284da1e93b36df61f1afc102ad6a913f19a6fde50e"
            ),
            policy_version="correction-followup-v1",
            reason_code="correction_followup_route_updated",
        ),
        "reply:tg:tawg:3560": _ReplyRepairSpec(
            trigger_id="tg:tawg:3560",
            trigger_sha256=("7e64cbc929d55e004b14c98062973eea9cea82e5e6b04fb97881c61379a65d42"),
            prepared_text_sha256=(
                "063486ea61377f4791c005fc9786f4c8335032ab79373e372463cf765b705e10"
            ),
            policy_version="stale-citation-context-v1",
            reason_code="stale_citation_context_repaired",
            requires_refusal=False,
        ),
        "reply:tg:tawg:3620": _ReplyRepairSpec(
            trigger_id="tg:tawg:3620",
            trigger_sha256=("9b455c63c9053fa84ba7e1d10323511bab8f97cfd6f75d0865180792540f6160"),
            prepared_text_sha256=(
                "31bf2825372e00933fa845ceebf1342d8eb016b04963f7259a15dfdf1dd1d2ac"
            ),
            policy_version="latest-discussion-v2",
            reason_code="latest_discussion_page_coverage_repaired",
            requires_refusal=False,
        ),
        "reply:tg:tawg:3650": _ReplyRepairSpec(
            trigger_id="tg:tawg:3650",
            trigger_sha256=("50cb62e71685f569c95682d589d210a1e7f8603846d207c15b1abaa6837b71fe"),
            prepared_text_sha256=(
                "dd96f9874a401e43b8570fc9d813d075ef3db7aa0753659b8079f8360ccd03bc"
            ),
            policy_version="latest-discussion-write-v1",
            reason_code="latest_discussion_knowledge_repaired",
            requires_refusal=False,
        ),
        "reply-repair:latest-discussion-write-v1:tg:tawg:3650": _ReplyRepairSpec(
            trigger_id="tg:tawg:3650",
            trigger_sha256=("50cb62e71685f569c95682d589d210a1e7f8603846d207c15b1abaa6837b71fe"),
            prepared_text_sha256=(
                "62fd221154ae7b2d4f55295a8e8e12b73110962e816da3fcac32918464cc0512"
            ),
            policy_version="latest-discussion-write-v2",
            reason_code="latest_discussion_knowledge_repaired",
            requires_refusal=False,
        ),
        "reply:tg:tawg:3668": _ReplyRepairSpec(
            trigger_id="tg:tawg:3668",
            trigger_sha256=("5eb89cb1053e5d3dccdab15844f034cffc1babd8331d69c0f1a952fe03f43406"),
            prepared_text_sha256=(
                "ce93546fb43d6d2a96b3a24a9c49bae9182b272deb0efcfe2acbcee853dd2c6d"
            ),
            policy_version="bot-health-evidence-v1",
            reason_code="unsupported_bot_recovery_claim",
            requires_refusal=False,
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
            repair_spec = self._LEGACY_REPAIRS.get(original.job_id)
            if (
                repair_spec is None
                or original.status is not JobStatus.DELIVERED
                or (repair_spec.requires_refusal and not original.refusal)
            ):
                continue
            trigger = records.get(repair_spec.trigger_id)
            prepared_text = original.prepared_reply_text
            if (
                original.trigger_record_id != repair_spec.trigger_id
                or trigger is None
                or trigger.content_sha256 != repair_spec.trigger_sha256
                or prepared_text is None
                or hashlib.sha256(prepared_text.encode("utf-8")).hexdigest()
                != repair_spec.prepared_text_sha256
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
                trigger_kind=original.trigger_kind,
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
                [jobs[job_id].persistence_payload() for job_id in sorted(jobs)],
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


class _KnowledgeWriteDecision(StrictModel):
    authorship: Literal["self_authored", "external"]
    authorship_evidence: list[str] = Field(min_length=1, max_length=32)
    original_url: str | None

    @field_validator("authorship_evidence")
    @classmethod
    def evidence_is_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("authorship evidence must be unique")
        return value


class _ReplyResult(StrictModel):
    schema_version: Literal["tawg.reply-result.v3"]
    reply_text: str
    language: str
    english_recap: str | None
    citations: list[str]
    evidence_status: Literal["verified", "partial", "not_verified"]
    verification_gaps: list[str]
    correction_transaction: VaultTransaction | None
    knowledge_write: _KnowledgeWriteDecision | None = None
    scan_registration: ScanRegistrationProposal | None = None
    refusal: bool


@dataclass(frozen=True, slots=True)
class PreparedReply:
    job_id: str
    reply_to_message_id: int | None
    message_thread_id: int | None
    reply_text: str
    citations: tuple[str, ...]
    language: str
    refusal: bool
    attachments: tuple[PreparedAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class _ReplyContext:
    text: str
    allowed_citations: frozenset[str]
    evidence_pack: EvidencePack | None
    mutation_capability: KnowledgeMutationCapability
    mutation_source_urls: frozenset[str]


@dataclass(frozen=True, slots=True)
class _LocalErcContext:
    citations: tuple[str, ...]
    pages: tuple[dict[str, Any], ...]
    source_keys: tuple[str, ...]
    verified_at: tuple[str, ...]


class BotReplyService:
    _ROUTER_VERSION = "contextual-ai-v5"
    _ROUTE_TIMEOUT_SECONDS = 60.0
    _ROUTE_CONTEXT_MAX_CHARS = 64_000
    _ROUTE_CONTEXT_MAX_PRIOR_RECORDS = 100
    _LATEST_DISCUSSION_KNOWLEDGE_PATH = "knowledge/topics/erc-8183-agentic-commerce.md"

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
        scan_target_verifier: ScanTargetVerifier | None = None,
        github_current_client: GitHubAnnouncementClient | None = None,
        invinoveritas_client: SafeJsonHttpClient | None = None,
        invinoveritas_api_key: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.ai = ai
        self.router = BotRouter(bot_username)
        self.live_evidence = live_evidence
        self.knowledge_state = knowledge_state
        if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
            raise ValueError("configured Telegram chat ID must be an integer")
        self.chat_id = chat_id
        self.max_budget_usd = max_budget_usd
        self.route_max_budget_usd = self._bounded_route_budget(
            max_budget_usd,
            route_max_budget_usd,
        )
        self.timeout_seconds = timeout_seconds
        self.scan_target_verifier = scan_target_verifier
        self.github_current_client = github_current_client
        # VERIFICATION route (invinoveritas /review + /verify-proof), added same day as the
        # standalone client (Telegram, damon group, msg 3817 Jimmy / msg 3823 Pavlo). Optional,
        # same injected-dependency shape as live_evidence/scan_target_verifier above -- when
        # unset, the VERIFICATION route below fails with a clear "not configured" error rather
        # than a crash, matching the existing live_evidence pattern.
        self.invinoveritas_client = invinoveritas_client
        self.invinoveritas_api_key = invinoveritas_api_key
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
        member_kinds = {
            TriggerKind.MEMBER_WELCOME,
            TriggerKind.MEMBER_INTRODUCTION,
        }
        if job.status is JobStatus.DELIVERED:
            return self._prepared(job)
        reuse_ready_introduction = (
            job.status is JobStatus.READY and job.trigger_kind is TriggerKind.MEMBER_INTRODUCTION
        )
        if job.status is JobStatus.READY and job.trigger_kind not in member_kinds:
            return self._prepared(job)
        if job.status is JobStatus.READY and not reuse_ready_introduction:
            reset_payload = job.model_dump(mode="json")
            reset_payload.update(
                {
                    "status": JobStatus.PENDING,
                    "prepared_reply_text": None,
                    "prepared_attachments": [],
                    "prepared_citations": [],
                    "prepared_language": None,
                    "refusal": False,
                    "classified_route": None,
                    "router_context_scope": None,
                    "router_context_sha256": None,
                    "router_version": None,
                    "routed_at": None,
                    "verification_artifact": None,
                    "knowledge_mutation_paths": [],
                    "knowledge_mutation_trigger_sha256": None,
                    "updated_at": now,
                }
            )
            job = PendingBotJob.model_validate(reset_payload)
            jobs[job_id] = job
        records = {record.record_id: record for record in SourceQuery(self.root).records()}
        trigger = records.get(job.trigger_record_id)
        if trigger is None:
            raise ReplyRejected("reply trigger evidence is missing")
        prerequisite = jobs.get(job.prerequisite_job_id or "")
        if job.trigger_kind in member_kinds and member_welcome_is_expired(
            job,
            trigger,
            now=now,
            prerequisite=prerequisite,
        ):
            cancelled_payload = job.model_dump(mode="json")
            cancelled_payload.update(
                {
                    "status": JobStatus.CANCELLED,
                    "prepared_reply_text": None,
                    "prepared_attachments": [],
                    "prepared_citations": [],
                    "prepared_language": None,
                    "refusal": False,
                    "classified_route": None,
                    "router_context_scope": None,
                    "router_context_sha256": None,
                    "router_version": None,
                    "routed_at": None,
                    "verification_artifact": None,
                    "knowledge_mutation_paths": [],
                    "knowledge_mutation_trigger_sha256": None,
                    "updated_at": now,
                    "safe_error_code": "member_welcome_expired",
                }
            )
            jobs[job_id] = PendingBotJob.model_validate(cancelled_payload)
            self._publish_jobs(jobs, f"{job_id}:cancelled")
            return None
        if reuse_ready_introduction:
            return self._prepared(job)
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
        scan_registry: ScanTargetRegistry | None = None
        scan_registry_changed = False
        failure_code = "reply_route_context_failed"
        try:
            if processing.trigger_kind in {
                TriggerKind.MEMBER_WELCOME,
                TriggerKind.MEMBER_INTRODUCTION,
            }:
                failure_code = "member_welcome_failed"
                return await self._prepare_member_welcome(
                    processing,
                    trigger,
                    records,
                    jobs,
                    now=now,
                    model_deadline=model_deadline,
                )
            context_builder = ConversationContextBuilder(self.privacy)
            route_context = context_builder.build(
                trigger=trigger,
                records=records.values(),
                message_thread_id=processing.message_thread_id,
                max_chars=self._ROUTE_CONTEXT_MAX_CHARS,
                max_prior_records=self._ROUTE_CONTEXT_MAX_PRIOR_RECORDS,
                trigger_kind=processing.trigger_kind,
            )
            routing_is_current = (
                processing.classified_route is not None
                and processing.router_context_scope is not None
                and processing.router_context_sha256 == route_context.sha256
                and processing.router_version == self._ROUTER_VERSION
            )
            if not routing_is_current:
                if processing.classified_route is not None:
                    processing = processing.model_copy(
                        update={
                            "classified_route": None,
                            "router_context_scope": None,
                            "router_context_sha256": None,
                            "router_version": None,
                            "routed_at": None,
                        }
                    )
                    jobs[job_id] = processing
                    self._publish_jobs(jobs, f"{job_id}:rerouting")
                failure_code = "reply_route_model_failed"
                decision = None
                for route_attempt in range(2):
                    try:
                        decision = await ContextualAiRouter(self.ai).classify(
                            route_context,
                            operation_id=f"{job_id}:route",
                            max_budget_usd=self.route_max_budget_usd,
                            timeout_seconds=min(
                                self._ROUTE_TIMEOUT_SECONDS,
                                self._remaining_model_time(model_deadline),
                            ),
                        )
                        break
                    except AiRouteRejected:
                        raise
                    except ClaudeCliError as error:
                        if route_attempt or not self._retryable_route_failure(error):
                            raise
                assert decision is not None
                route = self.router.authorize_ai_route(
                    decision.route,
                    processing.trigger_kind,
                )
                context_scope = decision.context_scope
                processing = processing.model_copy(
                    update={
                        "classified_route": route,
                        "router_context_scope": context_scope,
                        "router_context_sha256": decision.context_sha256,
                        "router_version": self._ROUTER_VERSION,
                        "routed_at": now,
                        "verification_artifact": decision.artifact,
                    }
                )
                jobs[job_id] = processing
                self._publish_jobs(jobs, f"{job_id}:routed")
            else:
                assert processing.classified_route is not None
                assert processing.router_context_scope is not None
                route = processing.classified_route
                context_scope = processing.router_context_scope

            require_correction_transaction = (
                processing.repair_reason_code == "latest_discussion_knowledge_repaired"
            )
            required_revision_paths = (
                (self._LATEST_DISCUSSION_KNOWLEDGE_PATH,) if require_correction_transaction else ()
            )
            if require_correction_transaction and route is not BotRoute.KNOWLEDGE_CORRECTION:
                failure_code = "reply_validation_failed"
                raise ReplyRejected("required knowledge correction has a non-correction route")

            if route is BotRoute.IGNORE:
                ignored = processing.model_copy(
                    update={
                        "status": JobStatus.IGNORED,
                        "prepared_reply_text": None,
                        "prepared_attachments": [],
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
                username = self.router.bot_username
                text = (
                    "I can't safely perform that action, so I didn't run it. "
                    "I can still help with the group's knowledge and coordination work.\n\n"
                    "Here are a few things I can help with:\n"
                    f"- **TAWG or ERC questions** — `@{username} what is ERC-8183?`\n"
                    "- **Record a concept on any subject** — "
                    f"`@{username} record our Garden Clock design in full`\n"
                    "- **Record an external concept with its source** — "
                    f"`@{username} record <concept>; original source: https://...`\n"
                    "- **TAWG-local identity corrections** — "
                    f"`@{username} please update this member's TAWG identity to ...`\n"
                    "- **Relevant source suggestions** — "
                    f"`@{username} please track this TAWG source: https://...`\n"
                    "- **Conversation follow-ups** — reply directly to one of my messages with "
                    "the missing detail or evidence.\n\n"
                    "If your request fits one of those, try one of the examples above and I'll "
                    "take another look."
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

            if route is BotRoute.VERIFICATION:
                # Narrowly-scoped exception inside the "external actions" refusal, per Pavlo
                # (damon msg 3823): explicit request over an identified artifact only. Short-
                # circuits BEFORE the shared knowledge/evidence pipeline below on purpose --
                # "no arbitrary egress and no silent sending of surrounding chat context" means
                # this must never pull in reply_chain/prior_messages, only the current trigger's
                # own text. That is also why this branch does not call context_builder.scope()
                # or _erc_query_for_context() the way every other non-terminal route does.
                failure_code = "reply_verification_failed"
                if self.invinoveritas_client is None:
                    raise ReplyRejected("invinoveritas verification is not configured")
                # The AI router extracts the artifact as a structured field from the natural
                # language trigger, rather than mechanically stripping a fixed "verify:" prefix.
                artifact = processing.verification_artifact
                artifact = self._validate_verification_artifact(artifact)
                try:
                    verification_result, proof_status = await verify_and_confirm(
                        self.invinoveritas_client,
                        artifact=artifact,
                        api_key=self.invinoveritas_api_key,
                    )
                except VerificationRejected as error:
                    raise ReplyRejected(str(error)) from error
                proof_attachment = None
                if proof_status.verified:
                    proof_attachment = build_verification_proof_attachment(
                        verification_result,
                        artifact=artifact,
                    )
                proof_filename = (
                    proof_attachment[0] if proof_attachment is not None else None
                )
                text = format_verification_reply(
                    verification_result,
                    proof_status,
                    artifact=artifact,
                    proof_filename=proof_filename,
                )
                prepared_attachments: list[PreparedAttachment] = []
                if proof_attachment is not None:
                    filename, content = proof_attachment
                    prepared_attachments.append(
                        PreparedAttachment(filename=filename, content=content)
                    )
                ready = processing.model_copy(
                    update={
                        "status": JobStatus.READY,
                        "prepared_reply_text": text,
                        "prepared_attachments": prepared_attachments,
                        "prepared_language": "en",
                        "prepared_citations": [],
                        # Not a "refusal" in the REFUSE-route sense (the request itself was in
                        # scope), but proof_status.verified=False means the verdict was withheld
                        # -- same fail-closed spirit Pavlo named, so it is not counted as a
                        # normal successful knowledge answer either.
                        "refusal": not proof_status.verified,
                        "updated_at": now,
                        "safe_error_code": None,
                    }
                )
                jobs[job_id] = ready
                publish_suffix = "verified" if proof_status.verified else "verification_withheld"
                self._publish_jobs(jobs, f"{job_id}:{publish_suffix}")
                return self._prepared(ready)

            failure_code = "reply_context_failed"
            reply_scope = context_builder.scope(
                trigger=trigger,
                records=records.values(),
                message_thread_id=processing.message_thread_id,
            )
            reply_chain = tuple(records[record_id] for record_id in reply_scope.reply_chain_ids)
            erc_query, erc_query_text = self._erc_query_for_context(
                trigger=trigger,
                reply_chain=reply_chain,
                route=route,
                context_scope=context_scope,
                trigger_kind=processing.trigger_kind,
            )
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
                    erc_query_text,
                    erc_query,
                    local_erc_citations,
                ):
                    if self.live_evidence is None or self.knowledge_state is None:
                        raise ReplyRejected("live ERC evidence is not configured")
                    failure_code = "reply_evidence_failed"
                    evidence_pack = await self.live_evidence.build(erc_query, now=now)
                    failure_code = "reply_context_failed"
            context = await self._context(
                trigger,
                records,
                processing,
                route,
                context_scope,
                jobs=jobs,
                evidence_pack=evidence_pack,
                local_erc_context=local_erc_context,
                reply_chain=reply_chain,
                scoped_record_ids=reply_scope.record_ids,
                require_correction_transaction=require_correction_transaction,
                required_revision_paths=required_revision_paths,
                now=now,
                model_deadline=model_deadline,
            )
            raw = self._already_satisfied_repair_result(
                processing,
                trigger,
                context,
            )
            if raw is None:
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
                mutation_capability=context.mutation_capability,
                mutation_source_urls=context.mutation_source_urls,
                require_correction_transaction=require_correction_transaction,
                required_revision_paths=required_revision_paths,
                now=now,
            )
            reply_text = result.reply_text.strip()
            if result.scan_registration is not None:
                try:
                    if self.scan_target_verifier is None:
                        raise ScanTargetRejected("scan target verification is unavailable")
                    target = await self.scan_target_verifier.verify(
                        result.scan_registration,
                        trigger_record_id=trigger.record_id,
                        now=now,
                    )
                    scan_registry, scan_registry_changed = ScanTargetStore(self.root).merged(target)
                except ScanTargetRejected:
                    scan_registry = None
                    scan_registry_changed = False
                    reply_text = (
                        f"{reply_text.rstrip()}\n\n{self._scan_registration_warning(trigger)}"
                    )
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
            if evidence_pack is not None:
                assert self.knowledge_state is not None
                self._persist_evidence_outcome(
                    evidence_pack,
                    now=now,
                    operation_id=f"{job_id}:evidence",
                )
            uow = RepositoryUnitOfWork(self.root, operation_id=safe_operation_id(job_id))
            external_texts = (
                tuple(item.text for item in evidence_pack.evidence)
                if evidence_pack is not None
                else ()
            )
            uow.register_external_evidence(external_texts)
            verified_external_locators = (
                tuple(item.citation_url for item in evidence_pack.evidence)
                if evidence_pack is not None
                else ()
            )
            persisted_reply_locators = tuple(
                citation
                for existing_job_id, existing_job in jobs.items()
                if existing_job_id != job_id
                for citation in existing_job.prepared_citations
                if citation.startswith("https://")
            )
            trusted_reply_locators = tuple(
                dict.fromkeys((*verified_external_locators, *persisted_reply_locators))
            )
            if trusted_reply_locators:
                uow.register_trusted_source_locators(trusted_reply_locators)
            if evidence_pack is None and route is BotRoute.SOURCE_SUGGESTION:
                if self.knowledge_state is None:
                    raise ReplyRejected("source candidate state is not configured")
                urls = self._suggested_urls(trigger.text_original)
                if not urls:
                    raise ReplyRejected("source suggestion has no safe URL")
                self.knowledge_state.add_candidates(uow, urls, trigger.record_id, now)
                proposal = self._proposed_scan_target(trigger.text_original)
                if proposal is not None:
                    if self.scan_target_verifier is None:
                        raise ReplyRejected("scan target verification is unavailable")
                    try:
                        target = await self.scan_target_verifier.verify(
                            proposal,
                            trigger_record_id=trigger.record_id,
                            now=now,
                        )
                        scan_registry, scan_registry_changed = ScanTargetStore(
                            self.root
                        ).merged(target)
                    except ScanTargetRejected:
                        scan_registry = None
                        scan_registry_changed = False
            if result.correction_transaction is not None:
                if (
                    route is BotRoute.KNOWLEDGE_CORRECTION
                    and len(result.correction_transaction.writes) > 3
                ):
                    raise ReplyRejected("correction changed too many knowledge paths")
                uses_general_source_urls = self._transaction_has_source_urls(
                    result.correction_transaction
                )
                scoped_urls = frozenset(
                    (context.mutation_source_urls if uses_general_source_urls else frozenset())
                    | (
                        frozenset(local_erc_context.citations)
                        if local_erc_context is not None
                        else frozenset()
                    )
                )
                citation_scope = (
                    CitationScope(
                        source_keys=(
                            frozenset(local_erc_context.source_keys)
                            if local_erc_context is not None
                            else frozenset()
                        ),
                        urls=scoped_urls,
                    )
                    if local_erc_context is not None or scoped_urls
                    else None
                )
                changed_paths = CorrectionService(
                    VaultTransactionEngine(self.root, citation_scope=citation_scope)
                ).stage(
                    result.correction_transaction,
                    operation_id=job_id,
                    uow=uow,
                )
                if required_revision_paths and (
                    len(changed_paths) != len(required_revision_paths)
                    or set(changed_paths) != set(required_revision_paths)
                ):
                    raise ReplyRejected("required knowledge correction is a no-op")
                if changed_paths and route is BotRoute.KNOWLEDGE_CORRECTION:
                    ready_payload = ready.model_dump(mode="json")
                    ready_payload.update(
                        {
                            "knowledge_mutation_paths": list(changed_paths),
                            "knowledge_mutation_trigger_sha256": (trigger.content_sha256),
                        }
                    )
                    ready = PendingBotJob.model_validate(ready_payload)
                    jobs[job_id] = ready
            if scan_registry is not None and scan_registry_changed:
                ScanTargetStore(self.root).stage(uow, scan_registry)
            self._stage_jobs(uow, jobs)
            uow.publish()
            return self._prepared(ready)
        except Exception as error:
            safe_error_code = self._safe_failure_code(failure_code, error)
            print(
                "tawg_event=reply_prepare_error "
                f"type={type(error).__name__} code={safe_error_code} "
                f"detail={str(error)[:240]!r}",
                flush=True,
            )
            failed_jobs = self._load_jobs()
            current = failed_jobs[job_id]
            failed_jobs[job_id] = current.model_copy(
                update={
                    "status": JobStatus.PENDING,
                    "updated_at": now,
                    "safe_error_code": safe_error_code,
                }
            )
            failure_uow = RepositoryUnitOfWork(
                self.root, operation_id=safe_operation_id(f"{job_id}:failed")
            )
            failure_uow.register_external_evidence(())
            self._stage_jobs(failure_uow, failed_jobs)
            failure_uow.publish()
            if evidence_pack is not None and self.knowledge_state is not None:
                self._persist_evidence_outcome(
                    evidence_pack,
                    now=now,
                    operation_id=f"{job_id}:failed:evidence",
                )
            raise ReplyRejected(
                "reply preparation failed safely", safe_code=safe_error_code
            ) from None

    async def _prepare_member_welcome(
        self,
        job: PendingBotJob,
        trigger: SourceRecord,
        records: Mapping[str, SourceRecord],
        jobs: dict[str, PendingBotJob],
        *,
        now: datetime,
        model_deadline: float,
    ) -> PreparedReply:
        if job.welcome_target_person_id is None or job.welcome_target_record_id is None:
            raise ReplyRejected("member welcome target is incomplete")
        target = records.get(job.welcome_target_record_id)
        if target is None or target.author_person_id != job.welcome_target_person_id:
            raise ReplyRejected("member welcome target evidence is missing")
        aliases = AliasRegistry.from_yaml(self.root / "knowledge/meta/aliases.yml")
        resolved_target = resolve_member_welcome_target(
            trigger=trigger,
            aliases=aliases,
            records=dict(records),
            message_thread_id=job.message_thread_id,
        )
        if resolved_target is None or resolved_target != (
            job.welcome_target_person_id,
            job.welcome_target_record_id,
        ):
            raise ReplyRejected("member welcome trigger is no longer valid")
        identity = aliases.people.get(job.welcome_target_person_id)
        handles = identity.get("handles", {}) if isinstance(identity, dict) else {}
        telegram_handles = handles.get("telegram", []) if isinstance(handles, dict) else []
        if (
            not isinstance(telegram_handles, list)
            or len(telegram_handles) != 1
            or not isinstance(telegram_handles[0], str)
        ):
            raise ReplyRejected("member welcome target has no unique public handle")
        prerequisite = None
        if job.trigger_kind is TriggerKind.MEMBER_INTRODUCTION:
            prerequisite = jobs.get(job.prerequisite_job_id or "")
            if prerequisite is None or prerequisite.status is not JobStatus.DELIVERED:
                raise ReplyRejected("member introduction prerequisite is not delivered")
        existing_profile, existing_frontmatter = member_profile_snapshot(
            self.root,
            person_id=job.welcome_target_person_id,
        )
        if job.trigger_kind is TriggerKind.MEMBER_WELCOME:
            reply_text, context_hash, language = build_member_welcome_reply(
                trigger=trigger,
                target=target,
                identity=identity,
            )
            router_version = "template-member-welcome-v1"
        else:
            context_pack, context_hash, expected_mention = build_member_ai_context(
                job=job,
                trigger=trigger,
                target=target,
                identity=identity,
                existing_profile=existing_profile,
                existing_frontmatter=existing_frontmatter,
                prior_delivered_welcome=(
                    prerequisite.prepared_reply_text if prerequisite is not None else None
                ),
            )
            raw = await self.ai.run(
                job_type="reply",
                context_pack=context_pack,
                operation_id=safe_operation_id(job.job_id),
                max_budget_usd=self.max_budget_usd,
                timeout_seconds=self._remaining_model_time(model_deadline),
            )
            try:
                result = _ReplyResult.model_validate(raw)
            except ValidationError as error:
                raise ReplyRejected("invalid member introduction model output") from error
            reply_text = result.reply_text.strip()
            if not reply_text or result.refusal:
                raise ReplyRejected("member introduction model refused")
            mentions = {mention.casefold() for mention in _TELEGRAM_MENTION.findall(reply_text)}
            if mentions != {expected_mention.casefold()}:
                raise ReplyRejected("member introduction mention is unsafe")
            if _INLINE_CITATION.search(reply_text) or _UNSAFE_MEMBER_LOCATOR.search(reply_text):
                raise ReplyRejected("member introduction contains an unsafe locator")
            if len(reply_text) > 8192:
                raise ReplyRejected("member introduction is too long")
            language = result.language
            router_version = "ai-member-introduction-v1"
        self.privacy.assert_public(reply_text)
        ready_payload = job.model_dump(mode="json")
        ready_payload.update(
            {
                "status": JobStatus.READY,
                "prepared_reply_text": reply_text,
                "prepared_citations": [],
                "prepared_language": language,
                "refusal": False,
                "safe_error_code": None,
                "classified_route": BotRoute.COORDINATION,
                "router_context_scope": RouteContextScope.CONVERSATION,
                "router_context_sha256": context_hash,
                "router_version": router_version,
                "routed_at": now,
                "updated_at": now,
            }
        )
        ready = PendingBotJob.model_validate(ready_payload)
        jobs[job.job_id] = ready
        self._publish_jobs(jobs, f"{job.job_id}:ready")
        return self._prepared(ready)

    @staticmethod
    def _validate_verification_artifact(artifact: str | None) -> str:
        """Gate placeholder for the extracted verification artifact.

        Reserved boundary so the real submission-format rules can land here after
        live testing. Today it only enforces the non-empty invariant; a rejected
        artifact still raises the same fail-closed ReplyRejected so the reply path
        degrades exactly as it did with the mechanical prefix extraction.
        """

        if not artifact or not artifact.strip():
            raise ReplyRejected("verification trigger has no text to verify")
        return artifact.strip()

    @staticmethod
    def _retryable_route_failure(error: ClaudeCliError) -> bool:
        message = str(error)
        return (
            message == "Claude Code structured output failed schema validation"
            or message == "Claude Code could not be started"
            or message.startswith("Claude Code failed with exit status")
        )

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
                "required knowledge correction has no transaction": (
                    "reply_required_correction_missing"
                ),
                "required knowledge correction has a non-correction route": (
                    "reply_required_correction_route_invalid"
                ),
                "required knowledge correction targets the wrong path": (
                    "reply_required_correction_target_invalid"
                ),
                "required knowledge correction has the wrong revision": (
                    "reply_required_correction_target_invalid"
                ),
                "required knowledge correction is a no-op": ("reply_required_correction_noop"),
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
        if message == "context pack exceeds its size limit":
            return "reply_context_too_large"
        if message == "context pack failed privacy validation":
            return "reply_context_privacy_failed"
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

    async def _context(
        self,
        trigger: SourceRecord,
        records: Mapping[str, SourceRecord],
        job: PendingBotJob,
        route: BotRoute,
        context_scope: RouteContextScope,
        *,
        jobs: Mapping[str, PendingBotJob],
        evidence_pack: EvidencePack | None,
        local_erc_context: _LocalErcContext | None = None,
        reply_chain: tuple[SourceRecord, ...] | None = None,
        scoped_record_ids: frozenset[str] | None = None,
        require_correction_transaction: bool = False,
        required_revision_paths: tuple[str, ...] = (),
        now: datetime,
        model_deadline: float,
    ) -> _ReplyContext:
        chain = (
            list(reply_chain)
            if reply_chain is not None
            else list(self._reply_chain(trigger, records))
        )
        seen = {trigger.record_id, *(record.record_id for record in chain)}
        use_scoped_conversation = (
            context_scope is RouteContextScope.CONVERSATION and scoped_record_ids is not None
        )

        nearby = [
            record
            for record in records.values()
            if record.created_at <= trigger.created_at
            and record.record_id not in seen
            and (scoped_record_ids is None or record.record_id in scoped_record_ids)
            and (
                use_scoped_conversation
                or abs(record.created_at - trigger.created_at) <= timedelta(minutes=30)
            )
        ]
        nearby.sort(key=ConversationContextBuilder.order_key)
        nearby = nearby[-50:]
        try:
            current_github = (
                await resolve_referenced_pull_evidence(
                    self.root,
                    (record.text_original for record in (trigger, *chain, *reversed(nearby[:50]))),
                    now=now,
                    client=self.github_current_client,
                    refresh_timeout_seconds=min(
                        8.0,
                        self._remaining_model_time(model_deadline),
                    ),
                )
                if route is BotRoute.KNOWLEDGE_QUESTION
                else ()
            )
        except GitHubAnnouncementRejected:
            raise ReplyRejected("current GitHub state is invalid") from None
        current_github_gaps = tuple(
            item for item in current_github if item.get("kind") != "github_pull_current_state"
        )
        current_github_states = tuple(
            item for item in current_github if item.get("kind") == "github_pull_current_state"
        )
        retrieval_query = self._retrieval_query(trigger, chain)
        retrieved_items = (
            []
            if context_scope is RouteContextScope.CONVERSATION
            else VaultRetriever(self.root).query(retrieval_query, top_k=16)
        )
        retrieved: list[dict[str, Any]] = (
            list(local_erc_context.pages) if local_erc_context is not None else []
        )
        for relative in required_revision_paths:
            target = self.root / relative
            knowledge_root = (self.root / "knowledge").resolve()
            if (
                target.is_symlink()
                or not target.is_file()
                or not target.resolve().is_relative_to(knowledge_root)
            ):
                raise ReplyRejected("required repair target is unavailable")
            try:
                current_bytes = target.read_bytes()
                current = current_bytes.decode("utf-8")
            except (OSError, UnicodeError):
                raise ReplyRejected("required repair target is unavailable") from None
            retrieved.append(
                {
                    "chunk_id": f"repair:{relative}",
                    "path": relative,
                    "text": current,
                    "record_id": "",
                    "source_locator": "",
                    "expected_sha256": hashlib.sha256(current_bytes).hexdigest(),
                }
            )
        privileged_paths = {page["path"] for page in retrieved if isinstance(page.get("path"), str)}
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
                if item.path not in privileged_paths
            ]
        )
        mutation_capability = build_mutation_capability(
            self.root,
            route=route,
            trigger=trigger,
            reply_chain=chain,
            retrieved_paths=(
                item["path"] for item in retrieved if isinstance(item.get("path"), str)
            ),
        )
        if required_revision_paths:
            revisions_by_path = {
                revision.path: revision for revision in mutation_capability.exact_revisions
            }
            if not set(required_revision_paths).issubset(revisions_by_path):
                raise ReplyRejected("required repair target is not mutation-authorized")
            mutation_capability = mutation_capability.model_copy(
                update={
                    "can_create_page": False,
                    "allowed_create_roots": (),
                    "exact_revisions": tuple(
                        revisions_by_path[path] for path in required_revision_paths
                    ),
                }
            )
        mutation_source_urls = (
            frozenset(extract_public_https_urls((trigger, *chain)))
            if route is BotRoute.KNOWLEDGE_CORRECTION
            else frozenset()
        )
        question_urls = (
            frozenset(extract_public_https_urls((trigger, *chain)))
            if route is BotRoute.KNOWLEDGE_QUESTION
            else frozenset()
        )
        current_github_urls: frozenset[str] = frozenset(
            cast(str, item["url"])
            for item in current_github_states
            if isinstance(item.get("url"), str)
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
        if route is BotRoute.KNOWLEDGE_QUESTION:
            local_ids.discard(trigger.record_id)
        allowed_citations: frozenset[str]
        citation_entries: list[dict[str, str]]
        if route is BotRoute.COORDINATION:
            allowed_citations = frozenset()
            citation_entries = []
        elif evidence_pack is not None:
            correction_local_ids = (
                ({trigger.record_id} | {record.record_id for record in chain}) & local_ids
                if route is BotRoute.KNOWLEDGE_CORRECTION
                else set()
            )
            allowed_citations = frozenset(
                correction_local_ids
                | set(evidence_pack.citation_allowlist)
                | set(mutation_source_urls)
                | set(current_github_urls)
            )
            citation_entries = [
                {
                    "record_id": record.record_id,
                    "source_locator": record.source_locator,
                }
                for record in sorted(
                    (records[record_id] for record_id in correction_local_ids),
                    key=lambda item: (item.created_at, item.record_id),
                )
            ]
            citation_entries.extend({"url": url} for url in evidence_pack.citation_allowlist)
            citation_entries.extend(
                {"url": url}
                for url in sorted(mutation_source_urls - set(evidence_pack.citation_allowlist))
            )
            citation_entries.extend(
                {"url": url}
                for url in sorted(current_github_urls - set(evidence_pack.citation_allowlist))
            )
        else:
            local_erc_citations = (
                local_erc_context.citations if local_erc_context is not None else ()
            )
            allowed_citations = frozenset(
                local_ids
                | set(local_erc_citations)
                | set(mutation_source_urls)
                | set(question_urls)
                | set(current_github_urls)
            )
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
            citation_entries.extend(
                {"url": url} for url in sorted(mutation_source_urls - set(local_erc_citations))
            )
            citation_entries.extend(
                {"url": url}
                for url in sorted(question_urls - set(local_erc_citations))
            )
            citation_entries.extend(
                {"url": url}
                for url in sorted(current_github_urls - set(local_erc_citations))
            )

        def record_context(record: SourceRecord) -> dict[str, Any]:
            payload = record.model_dump(mode="json")
            citation_urls = [
                url
                for url in extract_public_https_urls((record,))
                if url in mutation_source_urls or url in question_urls
            ]
            if citation_urls:
                payload["citation_urls"] = citation_urls
            return payload

        trigger_context: dict[str, Any] = {
            "route": route.value,
            "context_scope": context_scope.value,
            "record": record_context(trigger),
        }
        persisted_knowledge = self._persisted_knowledge(jobs, records, local_ids)
        if persisted_knowledge:
            trigger_context["persisted_knowledge"] = persisted_knowledge
        if current_github_states:
            trigger_context["github_current_state"] = list(current_github_states)
        if current_github_gaps:
            trigger_context["github_current_state_gaps"] = list(current_github_gaps)
        if evidence_pack is not None:
            trigger_context["erc_evidence_mode"] = "live"
        elif local_erc_context is not None:
            trigger_context["erc_evidence_mode"] = "local_synthesis"
            trigger_context["local_verified_at"] = list(local_erc_context.verified_at)
        if require_correction_transaction:
            trigger_context["required_action"] = "persist_correction_transaction_before_confirming"
        inputs = ContextInputs(
            trigger=trigger_context,
            reply_chain=[record_context(record) for record in chain],
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
                self.root / "src/tawg_bot/schemas/reply-result.v3.json"
            ),
            budgets={"max_output_chars": 8000, "max_citations": 16},
            evidence_pack=(
                self.privacy.sanitize_payload(evidence_pack.model_dump(mode="json"))
                if evidence_pack is not None
                else None
            ),
            citation_allowlist=list(allowed_citations),
            mutation_capability=mutation_capability.model_dump(mode="json"),
        )
        try:
            packed = ContextPackBuilder(self.privacy).build(
                inputs, max_chars=250_000, max_recent_telegram=50
            )
            return _ReplyContext(
                text=packed.text,
                allowed_citations=frozenset(packed.citation_allowlist),
                evidence_pack=evidence_pack,
                mutation_capability=mutation_capability,
                mutation_source_urls=mutation_source_urls,
            )
        except ContextRejected as error:
            raise ReplyRejected(str(error)) from None

    @staticmethod
    def _persisted_knowledge(
        jobs: Mapping[str, PendingBotJob],
        records: Mapping[str, SourceRecord],
        record_ids: set[str],
    ) -> list[dict[str, Any]]:
        matches: dict[str, set[str]] = {}
        ordered_jobs = sorted(
            jobs.values(),
            key=lambda item: (item.updated_at, item.job_id),
            reverse=True,
        )
        for job in ordered_jobs:
            trigger = records.get(job.trigger_record_id)
            if (
                job.trigger_record_id not in record_ids
                or job.status not in {JobStatus.READY, JobStatus.DELIVERED}
                or job.classified_route is not BotRoute.KNOWLEDGE_CORRECTION
                or not job.knowledge_mutation_paths
                or trigger is None
                or job.knowledge_mutation_trigger_sha256 != trigger.content_sha256
            ):
                continue
            matches.setdefault(job.trigger_record_id, set()).update(job.knowledge_mutation_paths)
            if len(matches) >= 50:
                break
        return [
            {"record_id": record_id, "paths": sorted(paths)[:3]}
            for record_id, paths in sorted(matches.items())
        ]

    @staticmethod
    def _reply_chain(
        trigger: SourceRecord,
        records: Mapping[str, SourceRecord],
    ) -> tuple[SourceRecord, ...]:
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
        return tuple(chain)

    def _erc_query_for_context(
        self,
        *,
        trigger: SourceRecord,
        reply_chain: tuple[SourceRecord, ...],
        route: BotRoute,
        context_scope: RouteContextScope,
        trigger_kind: TriggerKind,
    ) -> tuple[ErcQuery | None, str]:
        context_text = "\n\n".join(record.text_original for record in (*reply_chain, trigger))
        if context_scope is not RouteContextScope.ERC and not (
            route is BotRoute.KNOWLEDGE_CORRECTION and trigger_kind is TriggerKind.REPLY_TO_BOT
        ):
            return None, context_text
        query = self.router.erc_query(trigger.text_original)
        if query is not None or trigger_kind is not TriggerKind.REPLY_TO_BOT:
            return query, context_text
        human_chain = tuple(
            record
            for record in reply_chain
            if record.source_payload.get("message_kind") != "audited_bot_delivery"
            and record.source_payload.get("author_is_bot") is not True
        )
        audited_bot_chain = tuple(
            record
            for record in reply_chain
            if record.source_payload.get("message_kind") == "audited_bot_delivery"
        )
        for record in (*reversed(human_chain), *reversed(audited_bot_chain)):
            query = self.router.erc_query(record.text_original)
            if query is not None:
                return query, context_text
        return None, context_text

    @staticmethod
    def _already_satisfied_repair_result(
        job: PendingBotJob,
        trigger: SourceRecord,
        context: _ReplyContext,
    ) -> dict[str, Any] | None:
        if (
            job.repair_reason_code != "stale_citation_context_repaired"
            or job.classified_route is not BotRoute.KNOWLEDGE_CORRECTION
        ):
            return None
        required_evidence = tuple(
            record_id
            for record_id in context.mutation_capability.required_evidence
            if record_id != trigger.record_id and record_id in context.allowed_citations
        )
        for revision in context.mutation_capability.exact_revisions:
            frontmatter, _ = parse_frontmatter(revision.content)
            if frontmatter is None:
                continue
            title = frontmatter.get("title")
            source_ids = frontmatter.get("source_ids", [])
            telegram_record_ids = frontmatter.get("telegram_record_ids", [])
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(source_ids, list)
                or not isinstance(telegram_record_ids, list)
            ):
                continue
            recorded_evidence = {
                value for value in (*source_ids, *telegram_record_ids) if isinstance(value, str)
            }
            citation = next(
                (record_id for record_id in required_evidence if record_id in recorded_evidence),
                None,
            )
            if citation is None:
                continue
            return {
                "schema_version": "tawg.reply-result.v3",
                "reply_text": (
                    f"**{title.strip()}** is already recorded in the knowledge base and "
                    f"anchored to [{citation}]. No additional knowledge write was needed."
                ),
                "language": "en",
                "english_recap": None,
                "citations": [citation],
                "evidence_status": "verified",
                "verification_gaps": [],
                "correction_transaction": None,
                "knowledge_write": None,
                "scan_registration": None,
                "refusal": False,
            }
        return None

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
        attempts = self._load_delivery_attempts()
        current = trigger
        seen = {trigger.record_id}
        conversation_scope = trigger.record_id.rsplit(":", 1)[0]
        for depth in range(32):
            reply_targets = [
                relation.target_record_id
                for relation in current.relations
                if relation.relation_type == "reply_to"
            ]
            if len(reply_targets) != 1 or reply_targets[0] in seen:
                break
            target_record_id = reply_targets[0]
            if target_record_id.rsplit(":", 1)[0] != conversation_scope:
                raise ReplyRejected("direct reply target is outside Telegram scope")
            target_message_id = target_record_id.rsplit(":", 1)[-1]
            if depth == 0 and not target_message_id.isdigit():
                raise ReplyRejected("direct reply target is invalid")
            matching = (
                [
                    attempt
                    for attempt in attempts
                    if attempt.status is DeliveryStatus.DELIVERED
                    and attempt.telegram_chat_id == self.chat_id
                    and target_message_id.isdigit()
                    and int(target_message_id) in attempt.telegram_message_ids
                ]
                if target_message_id.isdigit()
                else []
            )
            if matching:
                if len(matching) != 1:
                    raise ReplyRejected("direct reply target lacks one audited bot delivery")
                audited = self._audited_bot_record(
                    target_record_id,
                    matching[0],
                    jobs,
                    child_created_at=current.created_at,
                    message_thread_id=job.message_thread_id,
                    webhook_text=(
                        current.source_payload.get("reply_to_message_text")
                        if depth == 0
                        else None
                    ),
                )
                if audited is None:
                    break
                augmented[target_record_id] = audited
                current = audited
                seen.add(target_record_id)
                continue
            if depth == 0:
                raise ReplyRejected("direct reply target lacks one audited bot delivery")
            parent = augmented.get(target_record_id)
            if parent is None:
                break
            current = parent
            seen.add(target_record_id)
        return augmented

    def _audited_bot_record(
        self,
        target_record_id: str,
        attempt: DeliveryAttempt,
        jobs: Mapping[str, PendingBotJob],
        *,
        child_created_at: datetime,
        message_thread_id: int | None,
        webhook_text: str | None = None,
    ) -> SourceRecord | None:
        if attempt.message_thread_id != message_thread_id:
            raise ReplyRejected("direct reply target failed thread audit binding")
        target_message_id = target_record_id.rsplit(":", 1)[-1]
        if not target_message_id.isdigit():
            raise ReplyRejected("direct reply target is invalid")
        if webhook_text is not None and webhook_text.strip():
            # The incoming webhook carries the replied-to message's text directly
            # (Telegram's `reply_to_message`), so a mirror worker can use it without
            # having to reconstruct it from the delivery state it does not persist.
            target_text = webhook_text
        else:
            delivered_text = self._audited_delivery_text(attempt, jobs)
            if delivered_text is None:
                return None
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
        if delivered_at >= child_created_at:
            raise ReplyRejected("direct reply target does not precede its trigger")
        return SourceRecord.from_text(
            record_id=target_record_id,
            source_type=SourceType.TELEGRAM_MESSAGE,
            source_locator=(
                f"repo:data/state/delivery-state.json#{attempt.delivery_id}:{target_message_id}"
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

    @staticmethod
    def _retrieval_query(trigger: SourceRecord, chain: list[SourceRecord]) -> str:
        human_chain = [
            record
            for record in chain
            if record.source_payload.get("message_kind") != "audited_bot_delivery"
        ]
        retrieval_chain = human_chain or chain
        text = "\n\n".join(
            record.text_original
            for record in (*retrieval_chain, trigger)
            if record.text_original.strip()
        )
        if len(text) <= 8000:
            return text
        return f"{text[:3999]}\n{text[-4000:]}"

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
        if attempt.reply_text is not None:
            # A receipt-only mirror worker does not persist its pending-bot-jobs, so the
            # audited reply text is carried on the delivery attempt itself. Verify it
            # against the recorded hash before trusting it.
            if attempt.content_sha256 != hashlib.sha256(
                attempt.reply_text.encode("utf-8")
            ).hexdigest():
                raise ReplyRejected("direct reply target failed delivery audit binding")
            return attempt.reply_text
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
        if attempt.content_sha256 != hashlib.sha256(delivered_text.encode("utf-8")).hexdigest():
            raise ReplyRejected("prepared Daily text failed delivery audit binding")
        return delivered_text

    def _load_delivery_attempts(self) -> tuple[DeliveryAttempt, ...]:
        attempts: list[DeliveryAttempt] = []
        for path in sorted((self.root / "data/state").glob("delivery-state*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError
                attempts.extend(DeliveryAttempt.model_validate(item) for item in raw)
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
        return tuple(attempts)

    def _local_erc_context(
        self,
        query: ErcQuery,
        *,
        include_revision: bool = False,
    ) -> _LocalErcContext | None:
        if self.knowledge_state is None:
            return None
        citations: list[str] = []
        pages: list[dict[str, Any]] = []
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
            if (
                not isinstance(source_keys, list)
                or not all(isinstance(source_key, str) for source_key in source_keys)
                or not isinstance(verified_at, str | datetime)
            ):
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
                "citation_urls": page_citations,
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
        mutation_capability: KnowledgeMutationCapability,
        mutation_source_urls: frozenset[str],
        require_correction_transaction: bool,
        required_revision_paths: tuple[str, ...],
        now: datetime,
    ) -> _ReplyResult:
        # `scan_registration` is only valid on the knowledge-correction route. A model
        # occasionally misreads a source suggestion (e.g. "add ERC 8380 to the follow-up
        # list") as a scan registration and returns a `scan_registration` whose
        # magicians_topic_url fails the ScanRegistrationProposal field validator, which
        # then aborts the whole reply as an "invalid reply model output". Drop that field
        # before validation on every non-correction route; the controller separately
        # rejects a scan_registration outside the correction route anyway.
        raw = dict(raw)
        if route is not BotRoute.KNOWLEDGE_CORRECTION:
            raw.pop("scan_registration", None)
        else:
            registration = raw.get("scan_registration")
            if isinstance(registration, dict) and isinstance(
                registration.get("magicians_topic_url"), str
            ):
                registration["magicians_topic_url"] = normalize_magicians_topic_url(
                    registration["magicians_topic_url"]
                )
                raw["scan_registration"] = registration
        try:
            result = _ReplyResult.model_validate(raw)
        except ValidationError as error:
            errors = [
                {"type": item["type"], "loc": item["loc"], "msg": item["msg"]}
                for item in error.errors()
            ]
            print(
                "tawg_event=reply_model_validation_error "
                f"errors={errors!r}",
                flush=True,
            )
            raise ReplyRejected("invalid reply model output") from error
        requester_non_english = bool(_NON_ENGLISH.search(trigger.text_original))
        if (
            not requester_non_english
            and result.language.casefold().startswith("en")
            and result.english_recap
        ):
            result = result.model_copy(update={"english_recap": None})
        normalized_reply_text = self._normalize_local_citation_rendering(
            result.reply_text, allowed_citations
        )
        normalized_english_recap = (
            self._normalize_local_citation_rendering(result.english_recap, allowed_citations)
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
        deduplicated_reply_text, deduplicated_english_recap = self._deduplicate_declared_citations(
            result.reply_text,
            result.english_recap,
            frozenset(result.citations),
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
        if require_correction_transaction and result.correction_transaction is None:
            raise ReplyRejected("required knowledge correction has no transaction")
        if result.correction_transaction is not None and (
            not correction_route or result.correction_transaction.operation_id != job_id
        ):
            raise ReplyRejected("reply attempted an unauthorized correction")
        if result.knowledge_write is not None and result.correction_transaction is None:
            raise ReplyRejected("knowledge write metadata has no transaction")
        if require_correction_transaction:
            assert result.correction_transaction is not None
            transaction = result.correction_transaction
            write_paths = tuple(write.path for write in transaction.writes)
            if len(write_paths) != len(required_revision_paths) or set(write_paths) != set(
                required_revision_paths
            ):
                raise ReplyRejected("required knowledge correction targets the wrong path")
            revisions_by_path = {
                revision.path: revision for revision in mutation_capability.exact_revisions
            }
            for write in transaction.writes:
                revision = revisions_by_path.get(write.path)
                if revision is None or write.expected_sha256 != revision.expected_sha256:
                    raise ReplyRejected("required knowledge correction has the wrong revision")
                if hashlib.sha256(write.content.encode("utf-8")).hexdigest() == (
                    revision.expected_sha256
                ):
                    raise ReplyRejected("required knowledge correction is a no-op")
        if result.scan_registration is not None:
            if route is not BotRoute.KNOWLEDGE_CORRECTION:
                raise ReplyRejected("scan registration is outside the correction route")
            registration_urls = {
                result.scan_registration.magicians_topic_url,
                *(
                    [result.scan_registration.proposal_pr_url]
                    if result.scan_registration.proposal_pr_url is not None
                    else []
                ),
            }
            normalized_source_urls = frozenset(
                normalize_magicians_topic_url(url) for url in mutation_source_urls
            )
            normalized_citations = frozenset(
                normalize_magicians_topic_url(url) for url in result.citations
            )
            if (
                not registration_urls.issubset(normalized_source_urls)
                or not registration_urls.issubset(normalized_citations)
                or result.scan_registration.erc_number
                not in self._explicit_erc_numbers(trigger.text_original)
            ):
                raise ReplyRejected("scan registration is not grounded in the trigger")
        if result.correction_transaction is not None:
            transaction = result.correction_transaction
            if route is BotRoute.KNOWLEDGE_CORRECTION:
                creates_page = any(write.expected_sha256 is None for write in transaction.writes)
                if creates_page and result.knowledge_write is None:
                    raise ReplyRejected("knowledge transaction omits authorship metadata")
                try:
                    validate_knowledge_transaction(
                        self.root,
                        transaction,
                        mutation_capability,
                    )
                except KnowledgeMutationRejected as error:
                    raise ReplyRejected(str(error)) from None
                if result.knowledge_write is not None:
                    knowledge_write = result.knowledge_write
                    try:
                        transaction = canonicalize_new_knowledge_transaction(
                            transaction,
                            now=now,
                            original_url=knowledge_write.original_url,
                        )
                    except KnowledgeMutationRejected as error:
                        raise ReplyRejected(str(error)) from None
                    result = result.model_copy(update={"correction_transaction": transaction})
                    self._validate_knowledge_write(
                        knowledge_write,
                        transaction,
                        mutation_capability=mutation_capability,
                        mutation_source_urls=mutation_source_urls,
                    )
            elif result.knowledge_write is not None:
                raise ReplyRejected("identity correction cannot write general knowledge")
            allowed_transaction_citations = allowed_citations | correction_source_keys
            if any(
                not set(write.citations).issubset(allowed_transaction_citations)
                for write in transaction.writes
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

    @staticmethod
    def _explicit_erc_numbers(text: str) -> frozenset[int]:
        return frozenset(
            int(value)
            for value in re.findall(r"\b(?:ERC|EIP)[-\s]?#?([1-9][0-9]{0,4})\b", text, re.I)
        )

    def _proposed_scan_target(self, text: str) -> ScanRegistrationProposal | None:
        """Deterministically parse a single "add ERC N + Magicians topic (+ PR)" request.

        The AI router is not reliable enough to emit ``scan_registration`` on the
        ``source_suggestion`` route, so the controller parses the trigger text itself. Returns
        None unless exactly one ERC number and one Magicians topic URL are present. A trailing
        Discourse post id (``/29274/17``) is stripped so the registry stores the canonical topic
        URL. The result is still passed through ``ScanTargetVerifier`` before it is persisted.
        """
        erc_numbers = self._explicit_erc_numbers(text)
        if len(erc_numbers) != 1:
            return None
        magicians = re.search(
            r"https://ethereum-magicians\.org/t/[a-z0-9][a-z0-9-]{0,199}/[1-9][0-9]{0,9}"
            r"(?:/[0-9]+)?",
            text,
            re.IGNORECASE,
        )
        if magicians is None:
            return None
        topic_url = normalize_magicians_topic_url(magicians.group(0))
        pr = re.search(
            r"https://github\.com/ethereum/ERCs/pull/[1-9][0-9]{0,9}",
            text,
            re.IGNORECASE,
        )
        return ScanRegistrationProposal(
            erc_number=next(iter(erc_numbers)),
            magicians_topic_url=topic_url,
            proposal_pr_url=pr.group(0) if pr is not None else None,
        )

    @staticmethod
    def _scan_registration_warning(trigger: SourceRecord) -> str:
        if _NON_ENGLISH.search(trigger.text_original):
            return (
                "知识内容已处理, 但周期扫描没有注册; 请核对 ERC 编号和原始 "
                "Magicians/提案 PR 链接后再发一次。"
            )
        return (
            "The knowledge content was handled, but the recurring scan was not registered. "
            "Please check the ERC number and original Magicians/proposal-PR links, then try "
            "that registration again."
        )

    @staticmethod
    def _validate_knowledge_write(
        decision: _KnowledgeWriteDecision,
        transaction: VaultTransaction,
        *,
        mutation_capability: KnowledgeMutationCapability,
        mutation_source_urls: frozenset[str],
    ) -> None:
        evidence = set(decision.authorship_evidence)
        if not evidence.issubset(mutation_capability.required_evidence):
            raise ReplyRejected("authorship cites evidence outside the audited conversation")
        if decision.authorship == "self_authored":
            if decision.original_url is not None:
                raise ReplyRejected("self-authored knowledge cannot claim an original URL")
            return

        original_url = decision.original_url
        if original_url is None or original_url not in mutation_source_urls:
            raise ReplyRejected("external knowledge requires its supplied original URL")
        for write in transaction.writes:
            if write.expected_sha256 is not None:
                continue
            frontmatter, body = parse_frontmatter(write.content)
            source_urls = frontmatter.get("source_urls") if frontmatter is not None else None
            if (
                not isinstance(source_urls, list)
                or original_url not in source_urls
                or original_url not in write.citations
                or original_url not in body
            ):
                raise ReplyRejected("external knowledge page omits its original source")
            description = re.split(
                r"(?m)^##\s+Sources\s*$",
                body,
                maxsplit=1,
            )[0].strip()
            if len(description) > 2_000:
                raise ReplyRejected("external knowledge description exceeds 2000 characters")

    @staticmethod
    def _transaction_has_source_urls(transaction: VaultTransaction) -> bool:
        for write in transaction.writes:
            frontmatter, _ = parse_frontmatter(write.content)
            if frontmatter is not None and isinstance(frontmatter.get("source_urls"), list):
                return True
        return False

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
        found.extend(match.rstrip(".,;:!?") for match in _URL_CITATION.findall(text))
        return tuple(found)

    @staticmethod
    def _normalize_local_citation_rendering(text: str, allowed_citations: frozenset[str]) -> str:
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
    def _deduplicate_declared_citations(
        reply_text: str,
        english_recap: str | None,
        declared_citations: frozenset[str],
    ) -> tuple[str, str | None]:
        seen: set[str] = set()

        def deduplicate(text: str) -> str:
            def replace_local(match: re.Match[str]) -> str:
                citation = match.group(1)
                if citation not in declared_citations:
                    return match.group(0)
                if citation in seen:
                    return ""
                seen.add(citation)
                return match.group(0)

            without_duplicate_local = _LOCAL_CITATION.sub(replace_local, text)

            def replace_url(match: re.Match[str]) -> str:
                rendered = match.group(0)
                raw_url = match.group("link") or match.group("autolink") or match.group("bare")
                citation = raw_url.rstrip(".,;:!?")
                if citation not in declared_citations:
                    return rendered
                if citation not in seen:
                    seen.add(citation)
                    return rendered
                if match.group("label") is not None:
                    return match.group("label")
                return raw_url[len(citation) :]

            return _RENDERED_URL_CITATION.sub(replace_url, without_duplicate_local)

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

    def _persist_evidence_outcome(
        self,
        evidence_pack: EvidencePack,
        *,
        now: datetime,
        operation_id: str,
    ) -> None:
        assert self.knowledge_state is not None
        uow = RepositoryUnitOfWork(self.root, operation_id=safe_operation_id(operation_id))
        uow.register_external_evidence(item.text for item in evidence_pack.evidence)
        try:
            self.knowledge_state.stage_evidence_outcome(
                uow, evidence_pack.for_persistence(), now=now
            )
            uow.publish()
        except PersistenceRejected:
            pass

    def _publish_jobs(self, jobs: Mapping[str, PendingBotJob], operation_id: str) -> None:
        uow = RepositoryUnitOfWork(self.root, operation_id=safe_operation_id(operation_id))
        uow.register_external_evidence(())
        self._stage_jobs(uow, jobs)
        uow.publish()

    @staticmethod
    def _stage_jobs(uow: RepositoryUnitOfWork, jobs: Mapping[str, PendingBotJob]) -> None:
        uow.stage_json(
            "data/state/pending-bot-jobs.json",
            [jobs[job_id].persistence_payload() for job_id in sorted(jobs)],
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
            attachments=tuple(job.prepared_attachments),
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
