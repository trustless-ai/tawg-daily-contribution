"""Deterministic, delivery-bound member welcome policy and knowledge mutation."""

from __future__ import annotations

import hashlib
import json
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
_JAPANESE = re.compile(r"[\u3040-\u30ff]")
_KOREAN = re.compile(r"[\uac00-\ud7af]")


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


def build_member_ai_context(
    *,
    job: PendingBotJob,
    trigger: SourceRecord,
    target: SourceRecord,
    identity: Mapping[str, Any] | None,
    existing_profile: str,
    existing_frontmatter: Mapping[str, Any] | None,
    prior_delivered_welcome: str | None,
) -> tuple[str, str, str]:
    mention = _public_mention(target=target, identity=identity)
    if job.trigger_kind not in {
        TriggerKind.MEMBER_WELCOME,
        TriggerKind.MEMBER_INTRODUCTION,
    }:
        raise MemberWelcomeRejected("unsupported member welcome phase")
    verified_profile = None
    if (
        existing_profile
        and existing_frontmatter is not None
        and existing_frontmatter.get("provenance_status") == "verified"
    ):
        if job.welcome_target_person_id is None:
            raise MemberWelcomeRejected("member person ID is missing")
        verified_profile = {
            "path": member_profile_path(job.welcome_target_person_id),
            "text": existing_profile,
        }
    context = {
        "trigger_kind": job.trigger_kind.value,
        "trigger": {
            "record_id": trigger.record_id,
            "text_original": trigger.text_original,
            "created_at": trigger.created_at.isoformat().replace("+00:00", "Z"),
        },
        "target": {
            "person_id": job.welcome_target_person_id,
            "record_id": target.record_id,
            "mention": mention,
            "text_original": target.text_original,
        },
        "verified_profile": verified_profile,
        "prior_delivered_welcome": prior_delivered_welcome,
    }
    rendered = json.dumps(context, ensure_ascii=False, sort_keys=True)
    return rendered, hashlib.sha256(rendered.encode("utf-8")).hexdigest(), mention


def build_member_welcome_reply(
    *,
    trigger: SourceRecord,
    target: SourceRecord,
    identity: Mapping[str, Any] | None,
) -> tuple[str, str, str]:
    mention = _public_mention(target=target, identity=identity)
    if _JAPANESE.search(trigger.text_original):
        reply = f"{mention}、Trustless AIへようこそ！👋"  # noqa: RUF001
        language = "ja"
    elif _KOREAN.search(trigger.text_original):
        reply = f"{mention}님, Trustless AI에 오신 것을 환영합니다! 👋"
        language = "ko"
    elif _CJK.search(trigger.text_original):
        reply = f"欢迎加入 Trustless AI，{mention}！👋"  # noqa: RUF001
        language = "zh"
    else:
        reply = f"{mention} Welcome to Trustless AI! 👋"
        language = "en"
    payload = "\n".join(
        (trigger.record_id, trigger.content_sha256, target.record_id, mention, reply)
    )
    return reply, hashlib.sha256(payload.encode("utf-8")).hexdigest(), language


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
    if "## Related topics" not in body:
        body = (
            f"{body.rstrip()}\n\n## Related topics\n\n"
            "Frequently referenced maintained standards: No maintained ERC page is "
            "strongly represented in the imported messages.\n\n"
            "This page is a navigation aid, not an identity claim outside this TAWG "
            "and not a contribution score. Detailed statements remain in the cited "
            "source records and can be corrected through the Bot.\n"
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
