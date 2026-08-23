"""Deterministic, read-only linting for the canonical Obsidian vault."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from tawg_bot.aliases import AliasError, AliasRegistry, InvalidAliasScope
from tawg_bot.ledger import (
    ClaimAssessment,
    ClaimAssessmentV2,
    EvidenceLedger,
    InsufficientEvidence,
)

_WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
_FENCED_CODE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)


@dataclass(frozen=True, slots=True)
class LintFinding:
    category: str
    severity: str
    path: str
    line: int | None
    message: str


@dataclass(frozen=True, slots=True)
class LintReport:
    findings: tuple[LintFinding, ...]

    @property
    def error_count(self) -> int:
        return sum(finding.severity == "error" for finding in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(finding.severity == "warning" for finding in self.findings)


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    try:
        raw = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text[end + 5 :]
    return (raw if isinstance(raw, dict) else None), text[end + 5 :]


class VaultLinter:
    _REQUIRED_FRONTMATTER = frozenset({"title", "type", "created", "updated"})

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.knowledge_root = self.root / "knowledge"

    def lint(
        self,
        *,
        overrides: Mapping[str, bytes] | None = None,
        now: datetime | None = None,
    ) -> LintReport:
        files = self._files(overrides or {})
        findings: list[LintFinding] = []
        pages: dict[str, str] = {}
        for path, payload in files.items():
            if not path.endswith(".md"):
                continue
            try:
                pages[path] = payload.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(
                    LintFinding(
                        "invalid_encoding",
                        "error",
                        path,
                        None,
                        "Markdown pages must use UTF-8",
                    )
                )
        page_names = self._page_names(pages)
        incoming = {path: 0 for path in pages}
        page_types: dict[str, str | None] = {}

        for path, text in sorted(pages.items()):
            relative = PurePosixPath(path)
            if len(relative.parts) >= 2 and tuple(
                part.casefold() for part in relative.parts[:2]
            ) == ("knowledge", "people"):
                findings.append(
                    LintFinding(
                        "legacy_acknowledgement_path",
                        "error",
                        path,
                        None,
                        "public member pages belong under knowledge/acknowledgements",
                    )
                )
            frontmatter, _ = parse_frontmatter(text)
            if frontmatter is None:
                findings.append(
                    LintFinding(
                        "frontmatter",
                        "error",
                        path,
                        1,
                        "missing or invalid YAML frontmatter",
                    )
                )
                page_types[path] = None
            else:
                page_types[path] = (
                    frontmatter.get("type") if isinstance(frontmatter.get("type"), str) else None
                )
                missing = sorted(self._REQUIRED_FRONTMATTER - set(frontmatter))
                if missing:
                    findings.append(
                        LintFinding(
                            "frontmatter",
                            "error",
                            path,
                            1,
                            f"missing required properties: {', '.join(missing)}",
                        )
                    )
                if any(isinstance(value, dict) for value in frontmatter.values()):
                    findings.append(
                        LintFinding(
                            "frontmatter",
                            "error",
                            path,
                            1,
                            "generated frontmatter must remain flat",
                        )
                    )
            scrubbed = _FENCED_CODE.sub("", text)
            for match in _WIKILINK.finditer(scrubbed):
                target = self._wikilink_target(match.group(1))
                matches = self._resolve_link(path, target, page_names)
                line = scrubbed.count("\n", 0, match.start()) + 1
                if not matches:
                    findings.append(
                        LintFinding(
                            "dead_link",
                            "error",
                            path,
                            line,
                            f"wikilink target not found: {target}",
                        )
                    )
                elif len(matches) > 1:
                    findings.append(
                        LintFinding(
                            "ambiguous_link",
                            "error",
                            path,
                            line,
                            f"wikilink target is ambiguous: {target}",
                        )
                    )
                else:
                    incoming[matches[0]] += 1

        for path, count in sorted(incoming.items()):
            if count or PurePosixPath(path).stem in {"index", "hot"} or page_types[path] == "meta":
                continue
            findings.append(
                LintFinding(
                    "orphan",
                    "warning",
                    path,
                    None,
                    "page has no incoming wikilinks; this may be intentional",
                )
            )

        findings.extend(self._lint_aliases(files))
        findings.extend(self._lint_ledgers(files, now or datetime.now(UTC)))
        findings.sort(key=lambda item: (item.path, item.line or 0, item.category, item.message))
        return LintReport(tuple(findings))

    def _files(self, overrides: Mapping[str, bytes]) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        if self.knowledge_root.exists():
            for path in sorted(self.knowledge_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(self.knowledge_root):
                    continue
                files[path.relative_to(self.root).as_posix()] = path.read_bytes()
        files.update(overrides)
        return files

    @staticmethod
    def _page_names(pages: Mapping[str, str]) -> dict[str, list[str]]:
        names: dict[str, list[str]] = {}
        for path in pages:
            relative = PurePosixPath(path).relative_to("knowledge").with_suffix("").as_posix()
            names.setdefault(relative.casefold(), []).append(path)
            names.setdefault(PurePosixPath(relative).name.casefold(), []).append(path)
        return names

    @staticmethod
    def _lint_aliases(files: Mapping[str, bytes]) -> list[LintFinding]:
        path = "knowledge/meta/aliases.yml"
        if path not in files:
            return []
        try:
            raw = yaml.safe_load(files[path])
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != "tawg.aliases.v1"
                or raw.get("scope") != "tawg-only"
                or not isinstance(raw.get("people", {}), dict)
            ):
                raise InvalidAliasScope("invalid TAWG alias registry")
            AliasRegistry(dict(raw.get("people", {})))
        except (AliasError, UnicodeError, yaml.YAMLError) as error:
            return [LintFinding("invalid_aliases", "error", path, None, str(error))]
        return []

    @staticmethod
    def _wikilink_target(raw: str) -> str:
        target = raw.split("|", 1)[0]
        target = target.split("#", 1)[0].split("^", 1)[0]
        return target.strip().removesuffix(".md")

    @staticmethod
    def _resolve_link(
        source_path: str, target: str, page_names: Mapping[str, list[str]]
    ) -> list[str]:
        del source_path
        key = target.strip("/").casefold()
        matches = page_names.get(key, [])
        return sorted(set(matches))

    def _lint_ledgers(self, files: Mapping[str, bytes], now: datetime) -> list[LintFinding]:
        findings: list[LintFinding] = []
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            return [
                LintFinding(
                    "invalid_ledger",
                    "error",
                    "knowledge/meta",
                    None,
                    "lint time must use UTC",
                )
            ]
        source_path = "knowledge/meta/source-ledger.json"
        claim_path = "knowledge/meta/claim-ledger.json"
        try:
            source_raw = json.loads(files[source_path])
            if not isinstance(source_raw, dict) or source_raw.get("schema") not in {
                "tawg.source-ledger.v1",
                "tawg.source-ledger.v2",
            }:
                raise ValueError("source ledger schema")
            entries = source_raw.get("entries")
            if not isinstance(entries, dict):
                raise ValueError("source ledger entries")
            evidence = EvidenceLedger.from_entries(entries, schema=source_raw["schema"])
        except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
            findings.append(LintFinding("invalid_ledger", "error", source_path, None, str(error)))
            return findings

        try:
            claim_raw = json.loads(files[claim_path])
            if not isinstance(claim_raw, dict) or claim_raw.get("schema") not in {
                "tawg.claim-ledger.v1",
                "tawg.claim-ledger.v2",
            }:
                raise ValueError("claim ledger schema")
            claims = claim_raw.get("entries")
            if not isinstance(claims, dict):
                raise ValueError("claim ledger entries")
            for claim_id, raw_claim in claims.items():
                if not isinstance(raw_claim, dict):
                    raise ValueError("claim ledger entry")
                embedded_id = raw_claim.get("claim_id")
                if embedded_id is not None and embedded_id != claim_id:
                    raise ValueError("claim ledger key does not match claim_id")
                payload = {"claim_id": claim_id, **raw_claim}
                claim_model = (
                    ClaimAssessment
                    if claim_raw["schema"] == "tawg.claim-ledger.v1"
                    else ClaimAssessmentV2
                )
                claim = claim_model.model_validate(payload)
                current_claim = claim.model_copy(
                    update={"assessed_at": max(claim.assessed_at, now)}
                )
                try:
                    evidence.validate_claim(current_claim)
                except InsufficientEvidence as error:
                    findings.append(
                        LintFinding("stale_support", "error", claim_path, None, str(error))
                    )
                except KeyError as error:
                    findings.append(
                        LintFinding("invalid_ledger", "error", claim_path, None, str(error))
                    )
        except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
            findings.append(LintFinding("invalid_ledger", "error", claim_path, None, str(error)))
        return findings
