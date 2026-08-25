from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import tawg_bot.runtime as runtime_module
from tawg_bot.daily import DailyReadiness, DailyService, DailyWindow
from tawg_bot.daily_evidence import DailyEvidenceCollector
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.runtime import _LivePipeline
from tawg_bot.storage import JsonlCollection

PROJECT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)
WINDOW = DailyWindow.for_due_run(NOW)
GITHUB_URL = "https://github.com/trustless-ai/agent-ercs/pull/42"
MAGICIANS_URL = "https://ethereum-magicians.org/t/erc-8004-trustless-agents/25098/7"


class RecordsClient:
    def __init__(self, records: tuple[SourceRecord, ...]) -> None:
        self.records = records
        self.calls: list[tuple[datetime, datetime]] = []

    async def collect_records(self, *, since: datetime, now: datetime) -> tuple[SourceRecord, ...]:
        self.calls.append((since, now))
        return self.records


class FakeAi:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.output)


def _record(
    record_id: str,
    source_type: SourceType,
    text: str,
    at: datetime,
    locator: str,
) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=source_type,
        source_locator=locator,
        author_person_id="alice",
        author_source_handle="alice",
        created_at=at,
        updated_at=at,
        text_original=text,
        ingested_at=NOW,
    )


def _seed(root: Path) -> None:
    for relative in (
        "config/privacy.yml",
        "config/bot-policy.yml",
        "knowledge/meta/sources.yml",
        "src/tawg_bot/schemas/daily-result.v1.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    (root / "knowledge/meta").mkdir(parents=True, exist_ok=True)
    (root / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    (root / "knowledge/index.md").write_text(
        "---\ntitle: Index\ntype: index\ncreated: 2026-08-23\nupdated: 2026-08-23\n"
        "---\n\n# Index\n\nGenerated orientation.\n",
        encoding="utf-8",
    )
    telegram = root / "data/telegram/2026/08/messages.jsonl"
    telegram.parent.mkdir(parents=True)
    telegram.write_bytes(
        JsonlCollection(telegram, SourceRecord).merged_bytes(
            [
                _record(
                    "tg:tawg:1",
                    SourceType.TELEGRAM_MESSAGE,
                    "Telegram contribution inside the window.",
                    WINDOW.start + timedelta(hours=2),
                    "repo:data/telegram/2026/08/messages.jsonl#tg:tawg:1",
                ),
                _record(
                    "tg:tawg:future",
                    SourceType.TELEGRAM_MESSAGE,
                    "Telegram item after the cutoff.",
                    WINDOW.end + timedelta(minutes=1),
                    "repo:data/telegram/2026/08/messages.jsonl#tg:tawg:future",
                ),
            ]
        )
    )
    state = root / "data/state"
    state.mkdir(parents=True)
    (state / "delivery-state.json").write_text("[]\n", encoding="utf-8")


def _active_output() -> dict[str, Any]:
    return json.loads((PROJECT / "tests/fixtures/ai/daily-active.json").read_text())


def _readiness() -> DailyReadiness:
    return DailyReadiness(
        telegram_synced_at=NOW,
        live_evidence_collected_at=NOW,
        knowledge_refreshed_at=NOW,
    )


@pytest.mark.asyncio
async def test_daily_collects_current_window_live_text_without_persisting_bodies(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    github = RecordsClient(
        (
            _record(
                "gh:agent-ercs:pull:42",
                SourceType.GITHUB_PULL_REQUEST,
                "GitHub live body canary: validation implementation advanced.",
                WINDOW.end - timedelta(hours=1),
                GITHUB_URL,
            ),
            _record(
                "gh:agent-ercs:old",
                SourceType.GITHUB_COMMIT,
                "Old GitHub activity.",
                WINDOW.start - timedelta(seconds=1),
                "https://github.com/trustless-ai/agent-ercs/commit/old",
            ),
        )
    )
    magicians = RecordsClient(
        (
            _record(
                "magicians:25098:post:7",
                SourceType.MAGICIANS_POST,
                "Magicians live body canary: reviewers clarified the trust boundary.",
                WINDOW.start + timedelta(hours=3),
                MAGICIANS_URL,
            ),
            _record(
                "magicians:25098:future",
                SourceType.MAGICIANS_POST,
                "Future Magicians activity.",
                WINDOW.end,
                "https://ethereum-magicians.org/t/25098/8",
            ),
        )
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    evidence = await DailyEvidenceCollector(tmp_path, github=github, magicians=magicians).collect(
        WINDOW, now=NOW
    )

    assert [item.evidence_id for item in evidence] == [
        "tg:tawg:1",
        "magicians:25098:post:7",
        "gh:agent-ercs:pull:42",
    ]
    assert github.calls == [(WINDOW.start, NOW)]
    assert magicians.calls == [(WINDOW.start, NOW)]
    ai = FakeAi(_active_output())
    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=_readiness(), evidence=evidence
    )
    assert prepared is not None
    context = ai.calls[0]["context_pack"]
    assert ai.calls[0]["timeout_seconds"] == 600
    assert "GitHub live body canary" in context
    assert "Magicians live body canary" in context
    assert "Old GitHub activity" not in context
    assert "Future Magicians activity" not in context
    trigger = json.loads(context)["trigger"]
    assert json.loads(context)["recent_telegram"] == []
    assert trigger["required_title"] == (
        "TAWG Daily Catch-up — 2026-08-22 23:00 UTC → 2026-08-23 23:00 UTC"
    )
    assert trigger["output_contract"] == {
        "citation_rule": (
            "Each direction may have one uncited synthesis sentence; every concrete What moved "
            "bullet starts with •, contains no other citation, and ends with exactly one exact "
            "allowlisted citation."
        ),
        "forbidden_terms": [
            "score",
            "leaderboard",
            "rank",
            "ranked",
            "ranking",
            "first place",
            "top contributor",
            "priority",
            "tier",
            "tiers",
            "winner",
            "winners",
            "MVP",
            "hero",
            "I did",
            "my work",
            "earned reward",
            "reward eligibility",
            "payout",
            "on-chain credit",
        ],
        "max_emoji": 10,
        "ordering_rule": (
            "Order directions and items by contribution impact and importance, without saying "
            "that anyone is ranked or scored."
        ),
        "required_sections": [
            "What moved",
            "Next up",
        ],
        "synthesis_rule": (
            "Each direction's first synthesis sentence may be uncited because its concrete "
            "supporting bullets immediately below are cited. It must not contain a URL or "
            "citation."
        ),
        "what_moved_rule": (
            "Integrate appreciation into each concrete item: name who did what, what it "
            "advanced, and why it helps the group or Trustless AI. Do not add a separate "
            "Appreciation section."
        ),
    }
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_daily_context_uses_bounded_evidence_excerpts(tmp_path: Path) -> None:
    _seed(tmp_path)
    record = _record(
        "gh:agent-ercs:pull:long",
        SourceType.GITHUB_PULL_REQUEST,
        "IMPORTANT-START " + ("x" * 4_000) + " IMPORTANT-END",
        WINDOW.end - timedelta(hours=1),
        GITHUB_URL,
    )
    evidence = DailyEvidenceCollector(
        tmp_path,
        github=RecordsClient(()),
        magicians=RecordsClient(()),
    )._convert(record)

    context = json.loads(DailyService(tmp_path, ai=FakeAi({}))._context(WINDOW, (evidence,)))
    excerpt = context["trigger"]["window_evidence"][0]["text"]

    assert excerpt.startswith("IMPORTANT-START")
    assert len(excerpt) <= 180
    assert "IMPORTANT-END" not in excerpt
    assert set(context["trigger"]["window_evidence"][0]) == {
        "author_person_id",
        "citation",
        "evidence_id",
        "source_kind",
        "text",
        "updated_at",
    }
    assert context["trigger"]["window_evidence"][0]["citation"] == GITHUB_URL
    assert context["citations"] == []


def test_daily_context_selection_bounds_sources_and_keeps_contributor_coverage(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    collector = DailyEvidenceCollector(
        tmp_path,
        github=RecordsClient(()),
        magicians=RecordsClient(()),
    )
    records = tuple(
        _record(
            f"tg:tawg:{index}",
            SourceType.TELEGRAM_MESSAGE,
            f"Alice contribution {index}: implemented ERC-{8000 + index} validation.",
            WINDOW.start + timedelta(minutes=index),
            f"repo:data/telegram/2026/08/messages.jsonl#tg:tawg:{index}",
        ).model_copy(update={"author_person_id": f"member-{index % 6}"})
        for index in range(30)
    ) + tuple(
        _record(
            f"gh:agent-ercs:commit:{index}",
            SourceType.GITHUB_COMMIT,
            f"GitHub contribution {index}: shipped implementation tests.",
            WINDOW.start + timedelta(hours=2, minutes=index),
            f"https://github.com/trustless-ai/agent-ercs/commit/{index}",
        ).model_copy(update={"author_person_id": f"developer-{index % 6}"})
        for index in range(24)
    )
    evidence = tuple(collector._convert(record) for record in records)

    selected = DailyService(tmp_path, ai=FakeAi({}))._select_context_evidence(evidence)

    assert len(selected) <= 14
    assert sum(item.source_kind == "telegram" for item in selected) <= 8
    assert sum(item.source_kind == "github" for item in selected) <= 8
    assert {item.author_person_id for item in selected if item.source_kind == "telegram"} == {
        f"member-{index}" for index in range(6)
    }
    assert {item.author_person_id for item in selected if item.source_kind == "github"} == {
        f"developer-{index}" for index in range(6)
    }


@pytest.mark.asyncio
async def test_quiet_day_requires_all_three_sources_to_be_empty(tmp_path: Path) -> None:
    _seed(tmp_path)
    telegram = tmp_path / "data/telegram/2026/08/messages.jsonl"
    telegram.write_text("", encoding="utf-8")
    empty = RecordsClient(())

    evidence = await DailyEvidenceCollector(tmp_path, github=empty, magicians=empty).collect(
        WINDOW, now=NOW
    )

    assert evidence == ()


@pytest.mark.asyncio
async def test_daily_skips_one_failed_live_source_without_losing_other_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)

    class FailedRecords:
        async def collect_records(
            self, *, since: datetime, now: datetime
        ) -> tuple[SourceRecord, ...]:
            del since, now
            raise RuntimeError("provider-secret-body")

    magicians = RecordsClient(
        (
            _record(
                "magicians:25098:post:7",
                SourceType.MAGICIANS_POST,
                "Available Magicians progress.",
                WINDOW.start + timedelta(hours=3),
                MAGICIANS_URL,
            ),
        )
    )

    evidence = await DailyEvidenceCollector(
        tmp_path,
        github=FailedRecords(),
        magicians=magicians,
    ).collect(WINDOW, now=NOW)

    assert [item.evidence_id for item in evidence] == [
        "tg:tawg:1",
        "magicians:25098:post:7",
    ]
    captured = capsys.readouterr().out
    assert "source=github" in captured
    assert "code=daily_source_failed" in captured
    assert "provider-secret-body" not in captured


@pytest.mark.asyncio
async def test_runtime_daily_composes_live_collectors_without_repository_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    github = RecordsClient(
        (
            _record(
                "gh:agent-ercs:pull:42",
                SourceType.GITHUB_PULL_REQUEST,
                "GitHub runtime body canary: implementation advanced.",
                WINDOW.end - timedelta(hours=1),
                GITHUB_URL,
            ),
        )
    )
    magicians = RecordsClient(
        (
            _record(
                "magicians:25098:post:7",
                SourceType.MAGICIANS_POST,
                "Magicians runtime body canary: reviewers clarified the boundary.",
                WINDOW.start + timedelta(hours=3),
                MAGICIANS_URL,
            ),
        )
    )

    monkeypatch.setattr(
        runtime_module,
        "GitHubActivityRecords",
        lambda root, *, client: github,
    )
    monkeypatch.setattr(
        runtime_module,
        "MagiciansActivityRecords",
        lambda root, *, client, registry: magicians,
    )

    class NoCheckpoint:
        async def publish(self, operation_id: str, root: Path) -> None:
            raise AssertionError(f"dry-run checkpointed {operation_id} in {root}")

    def snapshot() -> tuple[tuple[str, bool, bytes | None], ...]:
        return tuple(
            (
                path.relative_to(tmp_path).as_posix(),
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in sorted(tmp_path.rglob("*"))
        )

    ai = FakeAi(_active_output())
    async with httpx.AsyncClient() as client:
        pipeline = _LivePipeline(
            tmp_path,
            client=client,
            checkpoint=NoCheckpoint(),
            now=NOW,
            ai=ai,  # type: ignore[arg-type]
        )
        pipeline.telegram_synced_at = NOW
        pipeline.knowledge_refreshed_at = NOW
        before = snapshot()

        prepared = await pipeline.prepare_daily(WINDOW, dry_run=True)

    assert prepared is not None
    assert github.calls == [(WINDOW.start, NOW)]
    assert magicians.calls == [(WINDOW.start, NOW)]
    context = ai.calls[0]["context_pack"]
    assert "GitHub runtime body canary" in context
    assert "Magicians runtime body canary" in context
    assert snapshot() == before
    assert not (tmp_path / "data/github").exists()
    assert not (tmp_path / "data/magicians").exists()
    assert not (tmp_path / "data/state/prepared-daily.json").exists()
