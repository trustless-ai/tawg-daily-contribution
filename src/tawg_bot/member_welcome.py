"""Deterministic, delivery-bound member welcome policy and knowledge mutation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from tawg_bot.models import JobStatus, PendingBotJob, SourceRecord, TriggerKind
from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault import parse_frontmatter

_MAX_WELCOME_AGE = timedelta(hours=24)
_INTRODUCTION_L1_GRACE = timedelta(minutes=15)
_CJK = re.compile(r"[\u3400-\u9fff]")


class MemberWelcomeRejected(ValueError):
    """Raised when member-welcome state is incomplete or unsafe."""


def member_welcome_is_expired(
    job: PendingBotJob,
    trigger: SourceRecord,
    *,
    now: datetime,
    prerequisite: PendingBotJob | None = None,
) -> bool:
    if job.trigger_kind is TriggerKind.MEMBER_INTRODUCTION:
        return (
            prerequisite is None
            or prerequisite.status is not JobStatus.DELIVERED
            or now < prerequisite.updated_at
            or now - prerequisite.updated_at > _INTRODUCTION_L1_GRACE
        )
    age = now - trigger.created_at
    return age < timedelta(0) or age > _MAX_WELCOME_AGE


def member_profile_path(person_id: str) -> str:
    return f"knowledge/acknowledgements/{person_id}.md"


def member_profile_snapshot(
    root: Path,
    *,
    person_id: str,
) -> tuple[str, dict[str, Any] | None]:
    path = root / member_profile_path(person_id)
    if not path.is_file():
        return "", None
    current = path.read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(current)
    if frontmatter is None:
        raise MemberWelcomeRejected("existing member profile frontmatter is invalid")
    _validate_existing_profile(frontmatter)
    return current, frontmatter


def build_member_reply(
    *,
    job: PendingBotJob,
    trigger: SourceRecord,
    target: SourceRecord,
    identity: Mapping[str, Any] | None,
    existing_profile: str,
    existing_frontmatter: Mapping[str, Any] | None,
) -> tuple[str, str]:
    mention = _public_mention(target=target, identity=identity)
    chinese = _CJK.search(trigger.text_original) is not None
    if job.trigger_kind is TriggerKind.MEMBER_WELCOME:
        known_from_magicians = (
            existing_frontmatter is not None
            and existing_frontmatter.get("provenance_status") == "verified"
            and any(
                isinstance(source_id, str) and source_id.startswith("magicians:")
                for source_id in existing_frontmatter.get("source_ids", [])
            )
        )
        if chinese:
            reply = (
                f"{mention} 欢迎来到 TAWG！之前在 Ethereum Magicians 看到过你的贡献，"  # noqa: RUF001
                "很高兴这次能把这些经验也带到这里一起交流 👋"
                if known_from_magicians
                else f"{mention} 欢迎来到 TAWG，很高兴你加入我们！👋"  # noqa: RUF001
            )
        else:
            reply = (
                f"{mention} We've seen your contributions around Ethereum Magicians, and "
                "it's great to have that experience in TAWG too — welcome! 👋"
                if known_from_magicians
                else f"{mention} Welcome to TAWG — really glad to have you here! 👋"
            )
    elif job.trigger_kind is TriggerKind.MEMBER_INTRODUCTION:
        reply = (
            f"{mention} 顺便介绍一下 Trustless AI：我们是一群因为 ERC 标准在线认识的"  # noqa: RUF001
            "全球开发者、研究者和 EIP 作者，大家会把想法一起做成研究、标准和能跑的实现，"  # noqa: RUF001
            "目标是让 AI / 区块链结果可以被任何人独立验证或重算。看到感兴趣的话题直接聊、"
            "提想法或挑件想做的事就好。"
            if chinese
            else f"{mention} A bit of context on Trustless AI: we're people from around "
            "the world who met online around ERCs — builders, researchers, and EIP authors "
            "turning ideas into research, standards, and working implementations, with AI "
            "and blockchain results anyone can independently verify or recompute. Feel free "
            "to jump into a chat, propose something, or pick up whatever looks fun."
        )
    else:
        raise MemberWelcomeRejected("unsupported member welcome phase")
    context_payload = "\n".join(
        (
            job.trigger_kind.value,
            trigger.record_id,
            trigger.content_sha256,
            target.record_id,
            target.content_sha256,
            hashlib.sha256(existing_profile.encode("utf-8")).hexdigest(),
            reply,
        )
    )
    return reply, hashlib.sha256(context_payload.encode("utf-8")).hexdigest()


def stage_member_profile(
    root: Path,
    uow: RepositoryUnitOfWork,
    *,
    job: PendingBotJob,
    trigger: SourceRecord,
    target: SourceRecord,
    identity: Mapping[str, Any] | None,
    now: datetime,
) -> tuple[str, str]:
    if job.welcome_target_person_id is None:
        raise MemberWelcomeRejected("member person ID is missing")
    path = member_profile_path(job.welcome_target_person_id)
    target_path = root / path
    current = target_path.read_text(encoding="utf-8") if target_path.is_file() else ""
    frontmatter, body = parse_frontmatter(current)
    title = _safe_member_title(
        identity=identity,
        fallback_handle=target.author_source_handle or f"@{job.welcome_target_person_id}",
    )
    if target_path.is_file() and frontmatter is None:
        raise MemberWelcomeRejected("existing member profile frontmatter is invalid")
    if frontmatter is None:
        frontmatter = {
            "title": title,
            "type": "person",
            "created": now.date().isoformat(),
            "updated": now.date().isoformat(),
            "source_ids": [],
            "telegram_record_ids": [],
            "provenance_status": "verified",
        }
        body = ""
    _validate_existing_profile(frontmatter)
    frontmatter["updated"] = now.date().isoformat()
    for field in ("source_ids", "telegram_record_ids"):
        values = frontmatter.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise MemberWelcomeRejected("member profile provenance is invalid")
        frontmatter[field] = sorted(
            dict.fromkeys([*values, target.record_id, trigger.record_id])
        )
    if "## TAWG-local participation" not in body:
        body = (
            f"{body.rstrip()}\n\n## TAWG-local participation\n\n"
            f"Welcomed to the TAWG Telegram group on {now.date().isoformat()}.\n"
        ).lstrip()
    rendered = (
        "---\n"
        f"{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )
    uow.stage_bytes(path, rendered.encode("utf-8"))
    index_path = "knowledge/index.md"
    index = (root / index_path).read_text(encoding="utf-8")
    link = f"- [[acknowledgements/{Path(path).stem}|{frontmatter['title']}]]"
    uow.stage_bytes(index_path, _index_with_acknowledgement(index, link).encode("utf-8"))
    return path, index_path


def _public_mention(
    *,
    target: SourceRecord,
    identity: Mapping[str, Any] | None,
) -> str:
    handles = (identity or {}).get("handles", {})
    telegram_handles = handles.get("telegram", []) if isinstance(handles, dict) else []
    if (
        not isinstance(telegram_handles, list)
        or len(telegram_handles) != 1
        or not isinstance(telegram_handles[0], str)
    ):
        raise MemberWelcomeRejected("member has no unique public Telegram handle")
    stored = telegram_handles[0]
    source = target.author_source_handle or ""
    return (
        source
        if source.startswith("@") and source[1:].casefold() == stored.casefold()
        else f"@{stored}"
    )


def _validate_existing_profile(frontmatter: Mapping[str, Any]) -> None:
    if frontmatter.get("type") != "person":
        raise MemberWelcomeRejected("member profile target is not a person page")
    title = frontmatter.get("title")
    if not isinstance(title, str) or not _safe_wikilink_label(title):
        raise MemberWelcomeRejected("member profile title is unsafe")
    for field in ("source_ids", "telegram_record_ids"):
        values = frontmatter.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise MemberWelcomeRejected("member profile provenance is invalid")


def _safe_member_title(
    *,
    identity: Mapping[str, Any] | None,
    fallback_handle: str,
) -> str:
    display_names = [
        value
        for value in (identity or {}).get("display_names", [])
        if isinstance(value, str) and _safe_wikilink_label(value)
    ]
    if len(display_names) == 1:
        return display_names[0]
    if not _safe_wikilink_label(fallback_handle):
        raise MemberWelcomeRejected("member display name is unsafe")
    return fallback_handle


def _safe_wikilink_label(value: str) -> bool:
    return (
        value == value.strip()
        and 0 < len(value) <= 128
        and len(value.splitlines()) == 1
        and not any(character in "<>[]|" for character in value)
        and not any(
            unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            for character in value
        )
    )


def _index_with_acknowledgement(index: str, link: str) -> str:
    if link in index:
        return index
    heading = "## Acknowledgements"
    if heading not in index:
        return f"{index.rstrip()}\n\n{heading}\n\n{link}\n"
    lines = index.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (
            position
            for position in range(start, len(lines))
            if lines[position].startswith("## ")
        ),
        len(lines),
    )
    section = lines[start:end]
    links = sorted({item for item in section if item.startswith("- [[")} | {link})
    remainder = [item for item in section if not item.startswith("- [[")]
    while remainder and not remainder[-1].strip():
        remainder.pop()
    replacement = ["", *links]
    if remainder:
        replacement.extend(["", *remainder])
    lines[start:end] = replacement
    return "\n".join(lines).rstrip() + "\n"
