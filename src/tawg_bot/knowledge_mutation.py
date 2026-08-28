"""Controller-owned capability for evidence-backed interactive knowledge writes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from tawg_bot.models import BotRoute, SourceRecord, StrictModel
from tawg_bot.vault import frontmatter_is_mutation_evidence, parse_frontmatter
from tawg_bot.vault_transaction import VaultTransaction, VaultWrite

_CREATE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}\.md$")
_MAX_REVISIONS = 3
_MAX_REVISION_CHARACTERS = 60_000
_HTTPS_URL = re.compile(r"https://[^\s<>()\[\]]+", re.IGNORECASE)


class KnowledgeMutationRejected(ValueError):
    """Raised when a proposed knowledge write exceeds its exact capability."""


class ExactKnowledgeRevision(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=_MAX_REVISION_CHARACTERS)


class KnowledgeMutationCapability(StrictModel):
    can_create_page: bool
    allowed_create_roots: tuple[str, ...]
    exact_revisions: tuple[ExactKnowledgeRevision, ...]
    required_evidence: tuple[str, ...]
    trigger_record_id: str | None = None
    authorship_policy: Literal["explicit_only"] = "explicit_only"

    @classmethod
    def disabled(cls) -> KnowledgeMutationCapability:
        return cls(
            can_create_page=False,
            allowed_create_roots=(),
            exact_revisions=(),
            required_evidence=(),
            trigger_record_id=None,
        )


def extract_public_https_urls(records: Iterable[SourceRecord]) -> tuple[str, ...]:
    """Return stable public HTTPS URLs explicitly supplied in audited conversation evidence."""

    urls: list[str] = []
    for record in records:
        for match in _HTTPS_URL.findall(record.text_original):
            value = _clean_url_suffix(match)
            if _is_public_https_url(value):
                urls.append(value)
    return tuple(dict.fromkeys(urls))


def _clean_url_suffix(value: str) -> str:
    value = value.rstrip('.,;:!?)"]}')
    while value and (
        value[-1] == "\ufffd" or unicodedata.category(value[-1]) == "Cf"
    ):
        value = value[:-1]
    return value.rstrip('.,;:!?)"]}')


def _is_public_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.path and not parsed.path.startswith("/"))
        or any(ord(character) < 32 for character in value)
        or any(
            character == "\ufffd" or unicodedata.category(character) == "Cf"
            for character in value
        )
    ):
        return False
    normalized_host = host.casefold().rstrip(".")
    if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith(
        (".localhost", ".local")
    ):
        return False
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return "." in normalized_host
    return address.is_global


def canonicalize_new_knowledge_transaction(
    transaction: VaultTransaction,
    *,
    now: datetime,
    original_url: str | None,
) -> VaultTransaction:
    """Make new-page operational metadata controller-owned and deterministic."""

    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise KnowledgeMutationRejected("knowledge mutation time must use UTC")
    writes: list[VaultWrite] = []
    for write in transaction.writes:
        if write.expected_sha256 is not None:
            writes.append(write)
            continue
        relative = PurePosixPath(write.path)
        if len(relative.parts) != 3:
            raise KnowledgeMutationRejected("invalid new knowledge path")
        page_type = {
            "topics": "topic",
            "repos": "repository",
        }.get(relative.parts[1])
        if page_type is None:
            raise KnowledgeMutationRejected("invalid new knowledge root")
        frontmatter, body = parse_frontmatter(write.content)
        title = frontmatter.get("title") if frontmatter is not None else None
        if not isinstance(title, str) or not title.strip():
            heading = re.search(r"(?m)^#\s+(.+?)\s*$", body)
            title = heading.group(1).strip() if heading is not None else None
        if title is None or not title:
            raise KnowledgeMutationRejected("new knowledge page requires a title")

        local_citations = tuple(
            citation for citation in write.citations if citation.startswith("tg:")
        )
        source_urls = tuple(
            citation for citation in write.citations if citation.startswith("https://")
        )
        if original_url is not None and original_url not in source_urls:
            raise KnowledgeMutationRejected("new knowledge page omits its original URL")
        description = re.split(r"(?m)^##\s+Sources\s*$", body, maxsplit=1)[0].strip()
        date = now.date().isoformat()
        lines = [
            "---",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"type: {page_type}",
            f"created: {json.dumps(date)}",
            f"updated: {json.dumps(date)}",
            "source_ids:",
            *(f"- {json.dumps(value)}" for value in local_citations),
            "telegram_record_ids:",
            *(f"- {json.dumps(value)}" for value in local_citations),
        ]
        if source_urls:
            lines.extend(
                ["source_urls:", *(f"- {json.dumps(value)}" for value in source_urls)]
            )
        lines.extend(("provenance_status: verified", "---", "", description))
        if source_urls:
            lines.extend(("", "## Sources", ""))
            lines.extend(f"- {value}" for value in source_urls)
        content = "\n".join(lines).rstrip() + "\n"
        writes.append(write.model_copy(update={"content": content}))
    return transaction.model_copy(update={"writes": writes})


def build_mutation_capability(
    root: Path,
    *,
    route: BotRoute,
    trigger: SourceRecord,
    reply_chain: Iterable[SourceRecord],
    retrieved_paths: Iterable[str],
) -> KnowledgeMutationCapability:
    """Build the sole controller authority for one interactive knowledge mutation."""

    if route is not BotRoute.KNOWLEDGE_CORRECTION:
        return KnowledgeMutationCapability.disabled()
    evidence = tuple(
        dict.fromkeys(
            [trigger.record_id, *(record.record_id for record in reply_chain)]
        )
    )
    repository_root = root.resolve()
    revisions: list[ExactKnowledgeRevision] = []
    total_characters = 0
    seen: dict[str, str] = {}
    for raw_path in retrieved_paths:
        folded = raw_path.casefold()
        previous = seen.get(folded)
        if previous == raw_path:
            continue
        if previous is not None:
            raise KnowledgeMutationRejected("duplicate retrieved knowledge path")
        seen[folded] = raw_path
        if len(revisions) >= _MAX_REVISIONS:
            break
        relative = _eligible_revision_path(raw_path)
        if relative is None:
            continue
        target = repository_root.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file():
            continue
        resolved = target.resolve()
        if not resolved.is_relative_to(repository_root / "knowledge"):
            continue
        try:
            payload = target.read_bytes()
            content = payload.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        frontmatter, _ = parse_frontmatter(content)
        if frontmatter is None or not frontmatter_is_mutation_evidence(frontmatter):
            continue
        if total_characters + len(content) > _MAX_REVISION_CHARACTERS:
            continue
        revisions.append(
            ExactKnowledgeRevision(
                path=relative.as_posix(),
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                content=content,
            )
        )
        total_characters += len(content)
    return KnowledgeMutationCapability(
        can_create_page=True,
        allowed_create_roots=("knowledge/repos", "knowledge/topics"),
        exact_revisions=tuple(revisions),
        required_evidence=evidence,
        trigger_record_id=trigger.record_id,
    )


def validate_knowledge_transaction(
    root: Path,
    transaction: VaultTransaction,
    capability: KnowledgeMutationCapability,
) -> None:
    """Reject any transaction not exactly authorized by the supplied capability."""

    if capability.trigger_record_id is None:
        raise KnowledgeMutationRejected("knowledge mutation capability is disabled")
    exact = {revision.path: revision for revision in capability.exact_revisions}
    new_writes = [write for write in transaction.writes if write.expected_sha256 is None]
    if new_writes and (
        not capability.can_create_page
        or len(transaction.writes) != 1
        or len(new_writes) != 1
        or not _allowed_create_path(
            new_writes[0].path,
            capability.allowed_create_roots,
        )
    ):
        raise KnowledgeMutationRejected("unauthorized knowledge page creation")

    if len(transaction.writes) > _MAX_REVISIONS:
        raise KnowledgeMutationRejected("knowledge transaction exceeds revision limit")
    repository_root = root.resolve()
    for write in transaction.writes:
        if capability.trigger_record_id not in write.citations:
            raise KnowledgeMutationRejected("knowledge write omits trigger evidence")
        if write.expected_sha256 is None:
            continue
        revision = exact.get(write.path)
        if revision is None or write.expected_sha256 != revision.expected_sha256:
            raise KnowledgeMutationRejected("knowledge revision is outside capability")
        target = repository_root.joinpath(*PurePosixPath(write.path).parts)
        try:
            current_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as error:
            raise KnowledgeMutationRejected("stale knowledge revision") from error
        if current_sha256 != revision.expected_sha256:
            raise KnowledgeMutationRejected("stale knowledge revision")


def _eligible_revision_path(value: str) -> PurePosixPath | None:
    if "\\" in value or any(ord(character) < 32 for character in value):
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or len(path.parts) < 3
        or path.parts[0] != "knowledge"
        or path.parts[1].casefold() in {"acknowledgements", "meta"}
        or path.suffix.casefold() != ".md"
        or path.name.casefold() in {"hot.md", "index.md"}
    ):
        return None
    return path


def _allowed_create_path(value: str, allowed_roots: tuple[str, ...]) -> bool:
    path = _eligible_revision_path(value)
    if path is None or len(path.parts) != 3 or _CREATE_NAME.fullmatch(path.name) is None:
        return False
    return path.parent.as_posix() in allowed_roots
