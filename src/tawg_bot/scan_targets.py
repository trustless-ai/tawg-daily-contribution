"""Controller-owned registry for bounded recurring source discovery."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from tawg_bot.models import StrictModel

if TYPE_CHECKING:
    from tawg_bot.unit_of_work import RepositoryUnitOfWork

_MAGICIANS_PATH = re.compile(r"^/t/[a-z0-9][a-z0-9-]{0,199}/([1-9][0-9]{0,9})$")
_MAGICIANS_TOPIC_WITH_POST = re.compile(
    r"^(https://ethereum-magicians\.org/t/[a-z0-9][a-z0-9-]{0,199}/[1-9][0-9]{0,9})"
    r"(?:/[0-9]+)?$",
    re.IGNORECASE,
)
_PROPOSAL_PR_PATH = re.compile(r"^/ethereum/ERCs/pull/([1-9][0-9]{0,9})$")
_ERC_REFERENCE = r"(?:ERC|EIP)[- ]?{number}\b"
_MAX_EXTERNAL_METADATA_CHARACTERS = 64_000


def normalize_magicians_topic_url(value: str) -> str:
    """Strip an optional trailing Discourse post id from a Magicians topic URL.

    The pinned scan-target contract accepts only the canonical topic URL
    (``.../t/<slug>/<topic-id>``), but users and the AI frequently paste the full link
    with a trailing post id (``.../t/<slug>/<topic-id>/<post-id>``). Return the canonical
    form when the value already matches the topic shape; otherwise return it unchanged so
    the field validator can still reject genuinely invalid URLs.
    """
    match = _MAGICIANS_TOPIC_WITH_POST.fullmatch(value)
    return match.group(1) if match is not None else value


class ScanTargetRejected(ValueError):
    """Raised when scan target metadata violates the controller contract."""


class ScanTopicClient(Protocol):
    async def get_json(
        self,
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]: ...


class ScanGitHubClient(Protocol):
    async def get_json(
        self,
        path: str,
        params: dict[str, object] | None = None,
    ) -> list[Any] | dict[str, Any]: ...


def _validated_path(value: str, *, host: str, pattern: re.Pattern[str]) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold().rstrip(".") != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or pattern.fullmatch(parsed.path) is None
    ):
        raise ValueError("scan target URL is outside the allowed source boundary")
    return value


def magicians_topic_id(value: str) -> int:
    """Return the numeric Discourse topic ID from a validated URL."""

    match = _MAGICIANS_PATH.fullmatch(urlsplit(value).path)
    if match is None:
        raise ScanTargetRejected("invalid Magicians topic URL")
    return int(match.group(1))


def proposal_pr_number(value: str) -> int:
    """Return the pull request number from a validated ethereum/ERCs URL."""

    match = _PROPOSAL_PR_PATH.fullmatch(urlsplit(value).path)
    if match is None:
        raise ScanTargetRejected("invalid ethereum/ERCs proposal PR URL")
    return int(match.group(1))


class ScanRegistrationProposal(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    magicians_topic_url: str = Field(min_length=1, max_length=2048)
    proposal_pr_url: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("magicians_topic_url")
    @classmethod
    def magicians_url_is_scoped(cls, value: str) -> str:
        return _validated_path(
            value,
            host="ethereum-magicians.org",
            pattern=_MAGICIANS_PATH,
        )

    @field_validator("proposal_pr_url")
    @classmethod
    def proposal_pr_is_scoped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validated_path(value, host="github.com", pattern=_PROPOSAL_PR_PATH)


class ErcScanTarget(ScanRegistrationProposal):
    registered_from_record_id: str = Field(min_length=1, max_length=256)
    registered_at: datetime

    @field_validator("registered_at")
    @classmethod
    def registered_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("scan target registration timestamp must use UTC")
        return value.astimezone(UTC)


class ScanTargetRegistry(StrictModel):
    schema_version: Literal["tawg.scan-targets.v1"] = Field(alias="schema")
    github_organization: Literal["trustless-ai"]
    include_public_archived_repositories: Literal[True]
    ercs: list[ErcScanTarget] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def targets_are_unique(self) -> ScanTargetRegistry:
        erc_numbers: set[int] = set()
        topic_ids: set[int] = set()
        proposal_prs: set[int] = set()
        for target in self.ercs:
            topic_id = magicians_topic_id(target.magicians_topic_url)
            if target.erc_number in erc_numbers or topic_id in topic_ids:
                raise ValueError("scan targets must be unique by ERC number and topic ID")
            erc_numbers.add(target.erc_number)
            topic_ids.add(topic_id)
            if target.proposal_pr_url is not None:
                pull_number = proposal_pr_number(target.proposal_pr_url)
                if pull_number in proposal_prs:
                    raise ValueError("proposal PR scan targets must be unique")
                proposal_prs.add(pull_number)
        return self

    @classmethod
    def from_yaml_text(cls, text: str) -> ScanTargetRegistry:
        try:
            raw = yaml.safe_load(text)
            if not isinstance(raw, dict):
                raise ValueError("scan target registry root must be a mapping")
            return cls.model_validate(raw)
        except (UnicodeError, ValueError, ValidationError, yaml.YAMLError) as error:
            raise ScanTargetRejected("invalid scan target registry") from error

    def render_yaml(self) -> str:
        ordered = sorted(self.ercs, key=lambda target: target.erc_number)
        document = self.model_copy(update={"ercs": ordered})
        payload = document.model_dump(mode="json", by_alias=True)
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class ScanTargetStore:
    """Load and stage the single controller-owned scan registry."""

    PATH = "knowledge/meta/scan-targets.yml"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self) -> ScanTargetRegistry:
        path = self.root / self.PATH
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("scan target registry is not a regular file")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ScanTargetRejected("invalid scan target registry") from error
        return ScanTargetRegistry.from_yaml_text(text)

    def stage(self, uow: RepositoryUnitOfWork, registry: ScanTargetRegistry) -> None:
        if uow.root != self.root:
            raise ScanTargetRejected("unit of work root does not match scan target store")
        uow.stage_bytes(self.PATH, registry.render_yaml().encode("utf-8"))

    def register(self, uow: RepositoryUnitOfWork, target: ErcScanTarget) -> bool:
        """Stage one verified target, preserving the first identical registration."""

        registry, changed = self.merged(target)
        if changed:
            self.stage(uow, registry)
        return changed

    def merged(self, target: ErcScanTarget) -> tuple[ScanTargetRegistry, bool]:
        """Return the validated idempotent registry state for one verified target."""

        registry = self.load()
        existing = next(
            (item for item in registry.ercs if item.erc_number == target.erc_number),
            None,
        )
        if existing is not None:
            if (
                existing.magicians_topic_url != target.magicians_topic_url
                or existing.proposal_pr_url != target.proposal_pr_url
            ):
                raise ScanTargetRejected("ERC scan target conflicts with its registration")
            return registry, False
        updated = registry.model_copy(update={"ercs": [*registry.ercs, target]})
        try:
            updated = ScanTargetRegistry.model_validate(updated.model_dump(by_alias=True))
        except ValidationError as error:
            raise ScanTargetRejected("scan target conflicts with the registry") from error
        return updated, True


class ScanTargetVerifier:
    """Resolve and validate the bounded metadata behind a proposed ERC scan target."""

    def __init__(
        self,
        *,
        topic_client: ScanTopicClient,
        github_client: ScanGitHubClient | None,
    ) -> None:
        self.topic_client = topic_client
        self.github_client = github_client

    async def verify(
        self,
        proposal: ScanRegistrationProposal,
        *,
        trigger_record_id: str,
        now: datetime,
    ) -> ErcScanTarget:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ScanTargetRejected("scan target verification time must use UTC")
        topic_id = magicians_topic_id(proposal.magicians_topic_url)
        topic_slug = urlsplit(proposal.magicians_topic_url).path.split("/")[2]
        try:
            topic = await self.topic_client.get_json(f"/t/{topic_id}.json")
        except Exception:
            raise ScanTargetRejected("Magicians target could not be verified") from None
        self._verify_topic(
            topic,
            expected_id=topic_id,
            expected_slug=topic_slug,
            erc_number=proposal.erc_number,
        )
        if proposal.proposal_pr_url is not None:
            if self.github_client is None:
                raise ScanTargetRejected("proposal PR could not be verified")
            pull_number = proposal_pr_number(proposal.proposal_pr_url)
            try:
                pull = await self.github_client.get_json(
                    f"/repos/ethereum/ERCs/pulls/{pull_number}"
                )
            except Exception:
                raise ScanTargetRejected("proposal PR could not be verified") from None
            self._verify_pull(
                pull,
                expected_number=pull_number,
                erc_number=proposal.erc_number,
            )
        return ErcScanTarget(
            **proposal.model_dump(),
            registered_from_record_id=trigger_record_id,
            registered_at=now,
        )

    @staticmethod
    def _verify_topic(
        payload: Mapping[str, object],
        *,
        expected_id: int,
        expected_slug: str,
        erc_number: int,
    ) -> None:
        title = payload.get("title")
        slug = payload.get("slug")
        if (
            payload.get("id") != expected_id
            or slug != expected_slug
            or not isinstance(title, str)
            or len(title) > 512
            or re.search(_ERC_REFERENCE.format(number=erc_number), title, re.I) is None
        ):
            raise ScanTargetRejected("Magicians target does not match the requested ERC")

    @staticmethod
    def _verify_pull(
        payload: list[Any] | Mapping[str, object],
        *,
        expected_number: int,
        erc_number: int,
    ) -> None:
        if not isinstance(payload, Mapping):
            raise ScanTargetRejected("proposal PR metadata is invalid")
        title = payload.get("title")
        body = payload.get("body")
        body_text = body if isinstance(body, str) else ""
        if (
            payload.get("number") != expected_number
            or not isinstance(title, str)
            or len(title) + len(body_text) > _MAX_EXTERNAL_METADATA_CHARACTERS
            or re.search(
                _ERC_REFERENCE.format(number=erc_number),
                f"{title}\n{body_text}",
                re.I,
            )
            is None
        ):
            raise ScanTargetRejected("proposal PR does not match the requested ERC")
