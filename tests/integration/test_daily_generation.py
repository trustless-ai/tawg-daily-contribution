from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import tawg_bot.daily as daily_module
from tawg_bot.daily import DailyReadiness, DailyRejected, DailyService, DailyWindow
from tawg_bot.daily_evidence import DailyEvidence
from tawg_bot.models import SourceRecord, SourceType
from tawg_bot.storage import JsonlCollection

PROJECT = Path(__file__).parents[2]
WINDOW = DailyWindow.for_due_run(datetime(2026, 8, 24, 1, 17, tzinfo=UTC))


class FakeAi:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.output)


class SequenceAi:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.outputs[len(self.calls) - 1])


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((PROJECT / f"tests/fixtures/ai/{name}.json").read_text())


def _record(record_id: str, text: str, at: datetime) -> SourceRecord:
    return SourceRecord.from_text(
        record_id=record_id,
        source_type=SourceType.TELEGRAM_MESSAGE,
        source_locator=f"repo:data/telegram/2026/08/messages.jsonl#{record_id}",
        author_person_id="alice",
        author_source_handle="alice",
        created_at=at,
        updated_at=at,
        text_original=text,
        ingested_at=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )


def _seed(root: Path, *, active: bool = True, delivered: bool = False) -> None:
    for relative in (
        "config/privacy.yml",
        "config/bot-policy.yml",
        "src/tawg_bot/schemas/daily-result.v1.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / relative).read_bytes())
    (root / "knowledge/meta").mkdir(parents=True)
    (root / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n", encoding="utf-8"
    )
    (root / "knowledge/index.md").write_text(
        "---\ntitle: Index\ntype: index\ncreated: 2026-08-23\nupdated: 2026-08-23\n"
        "---\n\n# Index\n\nOpen validation questions remain.\n",
        encoding="utf-8",
    )
    source_path = root / "data/telegram/2026/08/messages.jsonl"
    source_path.parent.mkdir(parents=True)
    records = (
        [
            _record(
                "tg:tawg:1",
                "Alice clarified ERC-8004 validation behavior.",
                datetime(2026, 8, 23, 12, tzinfo=UTC),
            ),
        ]
        if active
        else []
    )
    records.append(
        _record(
            "tg:tawg:post-cutoff",
            "This belongs to tomorrow and must not appear.",
            datetime(2026, 8, 23, 23, 1, tzinfo=UTC),
        )
    )
    source_path.write_bytes(JsonlCollection(source_path, SourceRecord).merged_bytes(records))
    state = root / "data/state/delivery-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            [
                {
                    "schema_version": "tawg.delivery-attempt.v1",
                    "delivery_id": WINDOW.window_id,
                    "job_id": WINDOW.window_id,
                    "destination": "tawg",
                    "status": "delivered",
                    "telegram_message_ids": [42],
                    "prepared_at": "2026-08-24T00:00:00Z",
                    "updated_at": "2026-08-24T00:01:00Z",
                    "safe_error_code": None,
                }
            ]
            if delivered
            else []
        )
        + "\n",
        encoding="utf-8",
    )


def _ready() -> DailyReadiness:
    completed = datetime(2026, 8, 24, 1, tzinfo=UTC)
    return DailyReadiness(
        telegram_synced_at=completed,
        live_evidence_collected_at=completed,
        knowledge_refreshed_at=completed,
    )


def _confirm_alice_telegram_handle(root: Path) -> None:
    (root / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\n"
        "scope: tawg-only\n"
        "people:\n"
        "  alice:\n"
        "    display_names: [Alice]\n"
        "    handles:\n"
        "      telegram: [alice_tg]\n",
        encoding="utf-8",
    )


def _evidence(root: Path) -> tuple[DailyEvidence, ...]:
    path = root / "data/telegram/2026/08/messages.jsonl"
    records = JsonlCollection(path, SourceRecord).decode(path.read_bytes())
    return tuple(
        DailyEvidence(
            evidence_id=record.record_id,
            source_kind="telegram",
            source_url=record.source_locator,
            created_at=record.created_at,
            updated_at=record.updated_at,
            author_person_id=record.author_person_id,
            text=record.text_original,
        )
        for record in records
        if WINDOW.contains(record.updated_at)
    )


@pytest.mark.asyncio
async def test_active_daily_is_grounded_warm_english_and_excludes_post_cutoff(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    ai = FakeAi(_fixture("daily-active"))

    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert prepared.citations == ("tg:tawg:1",)
    assert len(prepared.messages) <= 2
    assert prepared.telegram_text.startswith("🗓 **TAWG Daily Catch-up**\n_")
    assert "## ⚡ **Highlights**" in prepared.telegram_text
    assert "## 🤝 **What moved**" in prepared.telegram_text
    assert "\n### agent-sdk\n" in prepared.telegram_text
    assert "\n- Alice — **Clarified**" in prepared.telegram_text
    assert "### ✅ **TODOs**" in prepared.telegram_text
    assert "## 🤖 **Trusty's take**" in prepared.telegram_text
    assert "**Today's spark:**" in prepared.telegram_text
    assert "post-cutoff" not in ai.calls[0]["context_pack"]
    assert "tg:tawg:1" in ai.calls[0]["context_pack"]


@pytest.mark.asyncio
async def test_daily_retries_one_rejected_model_draft_within_the_same_run(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    rejected = _fixture("daily-active")
    rejected["telegram_text"] = rejected["telegram_text"].replace(
        "🗓 **TAWG Daily Catch-up**", "🗓 **Wrong Daily title**", 1
    )
    accepted = _fixture("daily-active")
    ai = SequenceAi([rejected, accepted])

    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert prepared.telegram_text == accepted["telegram_text"]
    assert len(ai.calls) == 2


@pytest.mark.asyncio
async def test_daily_stops_after_two_rejected_model_drafts(tmp_path: Path) -> None:
    _seed(tmp_path)
    rejected = _fixture("daily-active")
    rejected["telegram_text"] = rejected["telegram_text"].replace(
        "🗓 **TAWG Daily Catch-up**", "🗓 **Wrong Daily title**", 1
    )
    ai = SequenceAi([rejected, rejected])

    with pytest.raises(DailyRejected, match="title"):
        await DailyService(tmp_path, ai=ai).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )

    assert len(ai.calls) == 2


@pytest.mark.asyncio
async def test_daily_retry_shares_the_original_model_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    rejected = _fixture("daily-active")
    rejected["telegram_text"] = rejected["telegram_text"].replace(
        "🗓 **TAWG Daily Catch-up**", "🗓 **Wrong Daily title**", 1
    )
    ai = SequenceAi([rejected, _fixture("daily-active")])
    times = iter((100.0, 111.0))
    monkeypatch.setattr(daily_module, "monotonic", lambda: next(times))

    with pytest.raises(DailyRejected, match="title"):
        await DailyService(tmp_path, ai=ai, timeout_seconds=10).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )

    assert len(ai.calls) == 1
    assert ai.calls[0]["timeout_seconds"] == 10


@pytest.mark.asyncio
async def test_daily_uses_confirmed_telegram_mention_for_mapped_contributor(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _confirm_alice_telegram_handle(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "- Alice — **Clarified**", "- Alice (@alice_tg) — **Clarified**"
    )
    ai = FakeAi(output)

    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    context = json.loads(ai.calls[0]["context_pack"])
    item = context["trigger"]["window_evidence"][0]
    assert item.get("contributor_label") == "Alice (@alice_tg)"
    assert "Alice (@alice_tg) — **Clarified**" in prepared.telegram_text


@pytest.mark.asyncio
async def test_daily_keeps_external_evidence_with_an_unmappable_author(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    evidence = (
        *_evidence(tmp_path),
        DailyEvidence(
            evidence_id="gh:tawg:commit:1",
            source_kind="github",
            source_url="https://github.com/trustless-ai/tawg-daily-contribution/commit/1",
            created_at=datetime(2026, 8, 23, 14, tzinfo=UTC),
            updated_at=datetime(2026, 8, 23, 14, tzinfo=UTC),
            author_person_id="tawg knowledge bot",
            text="The bot checkpointed its repository state.",
        ),
    )
    ai = FakeAi(_fixture("daily-active"))

    prepared = await DailyService(tmp_path, ai=ai).prepare(
        WINDOW, readiness=_ready(), evidence=evidence
    )

    assert prepared is not None
    context = json.loads(ai.calls[0]["context_pack"])
    external = next(
        item
        for item in context["trigger"]["window_evidence"]
        if item["source_kind"] == "github"
    )
    assert external["author_person_id"] == "tawg knowledge bot"
    assert "contributor_label" not in external


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contributor",
    ["Alice", "Alice (@not_alice)", "FakeAlice (@alice_tg)"],
)
async def test_daily_rejects_missing_or_unknown_mapped_contributor_mention(
    tmp_path: Path,
    contributor: str,
) -> None:
    _seed(tmp_path)
    _confirm_alice_telegram_handle(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "- Alice — **Clarified**", f"- {contributor} — **Clarified**"
    )

    with pytest.raises(DailyRejected, match="Telegram mention"):
        await DailyService(tmp_path, ai=FakeAi(output)).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )


@pytest.mark.asyncio
async def test_daily_rejects_borrowing_a_confirmed_handle_for_unmapped_evidence(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _confirm_alice_telegram_handle(tmp_path)
    evidence = (
        *_evidence(tmp_path),
        DailyEvidence(
            evidence_id="tg:tawg:2",
            source_kind="telegram",
            source_url="repo:data/telegram/2026/08/messages.jsonl#tg:tawg:2",
            created_at=datetime(2026, 8, 23, 13, tzinfo=UTC),
            updated_at=datetime(2026, 8, 23, 13, tzinfo=UTC),
            author_person_id="bob",
            text="Bob reviewed the validation edge cases.",
        ),
    )
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "- Alice — **Clarified**", "- Alice (@alice_tg) — **Clarified**"
    ).replace(
        "\n\n## 🚀 **Next up**",
        "\n- Bob (@alice_tg) — **Reviewed** the validation edge cases, helping the "
        "group find integration risks earlier. [tg:tawg:2]\n\n## 🚀 **Next up**",
    )
    output["citations"].append("tg:tawg:2")

    with pytest.raises(DailyRejected, match="Telegram mention"):
        await DailyService(tmp_path, ai=FakeAi(output)).prepare(
            WINDOW, readiness=_ready(), evidence=evidence
        )


@pytest.mark.asyncio
async def test_daily_rejects_a_confirmed_mention_outside_what_moved(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _confirm_alice_telegram_handle(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "- Alice — **Clarified**", "- Alice (@alice_tg) — **Clarified**"
    ).replace(
        "That's one less fog bank for the builders.",
        "Alice (@alice_tg), that's one less fog bank for the builders.",
    )

    with pytest.raises(DailyRejected, match="Telegram mention"):
        await DailyService(tmp_path, ai=FakeAi(output)).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )


@pytest.mark.asyncio
async def test_daily_rejects_a_citation_bound_to_conflicting_contributors(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (tmp_path / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\n"
        "scope: tawg-only\n"
        "people:\n"
        "  alice:\n"
        "    display_names: [Alice]\n"
        "    handles:\n"
        "      github: [alice-gh]\n"
        "      telegram: [alice_tg]\n"
        "  bob:\n"
        "    display_names: [Bob]\n"
        "    handles:\n"
        "      github: [bob-gh]\n"
        "      telegram: [bob_tg]\n",
        encoding="utf-8",
    )
    shared_url = "https://github.com/trustless-ai/agent-ercs/pull/42"
    evidence = tuple(
        DailyEvidence(
            evidence_id=f"gh:agent-ercs:review:{index}",
            source_kind="github",
            source_url=shared_url,
            created_at=datetime(2026, 8, 23, 13 + index, tzinfo=UTC),
            updated_at=datetime(2026, 8, 23, 13 + index, tzinfo=UTC),
            author_person_id=author,
            text=f"{name} reviewed the validation change.",
        )
        for index, (author, name) in enumerate(
            (("alice-gh", "Alice"), ("bob-gh", "Bob"))
        )
    )
    ai = FakeAi(_fixture("daily-active"))

    with pytest.raises(DailyRejected, match="conflicting contributor"):
        await DailyService(tmp_path, ai=ai).prepare(
            WINDOW, readiness=_ready(), evidence=evidence
        )

    assert not ai.calls


@pytest.mark.asyncio
async def test_quiet_daily_is_still_warm_without_inventing_progress(tmp_path: Path) -> None:
    _seed(tmp_path, active=False)

    prepared = await DailyService(tmp_path, ai=FakeAi(_fixture("daily-quiet"))).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert prepared.quiet_day
    assert "No source-backed progress landed" in prepared.telegram_text
    assert prepared.citations == ()


@pytest.mark.asyncio
async def test_delivered_window_is_not_regenerated(tmp_path: Path) -> None:
    _seed(tmp_path, delivered=True)
    ai = FakeAi(_fixture("daily-active"))

    assert (
        await DailyService(tmp_path, ai=ai).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )
        is None
    )
    assert not ai.calls


@pytest.mark.asyncio
async def test_daily_requires_fresh_success_from_every_required_layer(tmp_path: Path) -> None:
    _seed(tmp_path)
    stale = datetime(2026, 8, 23, 22, 59, tzinfo=UTC)
    readiness = _ready()
    readiness = DailyReadiness(
        telegram_synced_at=readiness.telegram_synced_at,
        live_evidence_collected_at=stale,
        knowledge_refreshed_at=readiness.knowledge_refreshed_at,
    )
    ai = FakeAi(_fixture("daily-active"))

    with pytest.raises(DailyRejected, match="fresh"):
        await DailyService(tmp_path, ai=ai).prepare(
            WINDOW, readiness=readiness, evidence=_evidence(tmp_path)
        )

    assert not ai.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(telegram_text=value["telegram_text"] + " 我完成了"), "English"),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"] + " 🥇🏆🎉🔥✨🎯🥇🏆🎉✨"
            ),
            "emoji",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"] + "\n- Alice tops the leaderboard."
            ),
            "ranking",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"] + "\nAlice ranked first today."
            ),
            "ranking",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "### agent-sdk", "### agent-sdk — priority 1"
                )
            ),
            "ranking",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "### agent-sdk", "### Tier 1 winner: agent-sdk"
                )
            ),
            "ranking",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "### agent-sdk", "### agent-sdk — Priority: 1"
                )
            ),
            "ranking",
        ),
        (lambda value: value.update(citations=["made-up:1"]), "citation"),
    ],
)
async def test_daily_rejects_language_persona_and_evidence_policy_violations(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    _seed(tmp_path)
    output = _fixture("daily-active")
    mutation(output)

    with pytest.raises(DailyRejected, match=message):
        await DailyService(tmp_path, ai=FakeAi(output)).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "_2026-08-22 23:00 → 2026-08-23 23:00 UTC_",
                    "_2026-08-22 23:00 → 2026-08-23 23:00 UTC — extra commentary_",
                    1,
                )
            ),
            "window",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "\n## 🤝 **What moved**\n", "\nToday, What moved matters.\n"
                )
            ),
            "section",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "\n## 🚀 **Next up**\n",
                    "\n## 🤝 **What moved**\n\n## 🚀 **Next up**\n",
                )
            ),
            "section",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "Trustless AI integration. [tg:tawg:1]",
                    "Trustless AI integration. [tg:tawg:1] trailing text",
                    1,
                )
            ),
            "citation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "Trustless AI integration. [tg:tawg:1]",
                    "Trustless AI integration. [tg:tawg:1] [tg:tawg:1]",
                    1,
                )
            ),
            "citation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "- Alice — **Clarified** ERC-8004",
                    "- Alice [made-up:next] — **Clarified** ERC-8004",
                )
            ),
            "citation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "- Alice — **Clarified** ERC-8004",
                    "- Alice [tg:tawg:1] — **Clarified** ERC-8004",
                )
            ),
            "citation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "Trustless AI integration. [tg:tawg:1]",
                    "Trustless AI integration [made-up:999]. [tg:tawg:1]",
                    1,
                )
            ),
            "citation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "- Alice — **Clarified** ERC-8004 validation behavior, advancing the next "
                    "implementation pass and giving the group a clearer path toward verifiable "
                    "Trustless AI integration. [tg:tawg:1]",
                    "• Alice clarified ERC-8004 validation behavior without a citation.",
                )
            ),
            "bullet",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "\n## 🚀 **Next up**\n",
                    "\n## 🙏 **Appreciation**\n\nThanks to everyone who contributed."
                    "\n\n## 🚀 **Next up**\n",
                )
            ),
            "Appreciation",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "_The validation direction became clearer and easier to build "
                    "on this window._\n",
                    "_The validation direction became clearer and easier to build "
                    "on this window._\n"
                    "\nAlice merged PR 42 and resolved the blocker without a citation.\n",
                )
            ),
            "structure",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "_The validation direction became clearer and easier to build on this window._",
                    "_The implementation details are at https://example.com/change._",
                )
            ),
            "synthesis",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "\n## 🚀 **Next up**\n",
                    "\n## 🙏 **Shout-outs**\n\nThanks to everyone who contributed."
                    "\n\n## 🚀 **Next up**\n",
                )
            ),
            "section",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "\n## 🤖 **Trusty's take**\n",
                    "\n**Shout-outs**\nThanks to everyone who contributed."
                    "\n\n## 🤖 **Trusty's take**\n",
                )
            ),
            "section",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "### ✅ **TODOs**", "### ✅ **Act**"
                )
            ),
            "section",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "> **Validation path clarified** — The next implementation pass has a "
                    "clearer direction. [tg:tawg:1]",
                    "> **Validation path clarified** — The next implementation pass has a "
                    "clearer direction.",
                )
            ),
            "highlight",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "> That's one less fog bank for the builders.",
                    "> Alice (@alice_tg), that's one less fog bank for the builders.",
                )
            ),
            "Telegram mention",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace(
                    "> That's one less fog bank for the builders.",
                    "> That's one less fog bank for the builders. [tg:tawg:1]",
                )
            ),
            "Trusty",
        ),
        (
            lambda value: value.update(
                telegram_text=value["telegram_text"].replace("\n>\n", "\n", 1)
            ),
            "Trusty",
        ),
    ],
)
async def test_daily_rejects_approximate_output_contracts(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    _seed(tmp_path)
    output = _fixture("daily-active")
    mutation(output)

    with pytest.raises(DailyRejected, match=message):
        await DailyService(tmp_path, ai=FakeAi(output)).prepare(
            WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
        )


@pytest.mark.asyncio
async def test_daily_allows_generic_review_language_in_uncited_synthesis(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "The validation direction became clearer and easier to build on this window.",
        "The validation direction moved into a clearer review phase.",
    )

    prepared = await DailyService(tmp_path, ai=FakeAi(output)).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert "clearer review phase" in prepared.telegram_text


@pytest.mark.asyncio
async def test_daily_allows_generic_progress_verbs_without_source_identifiers(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "The validation direction became clearer and easier to build on this window.",
        "The direction was implemented and tested, making the next review easier.",
    )

    prepared = await DailyService(tmp_path, ai=FakeAi(output)).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert "implemented and tested" in prepared.telegram_text


@pytest.mark.asyncio
async def test_daily_allows_uncited_direction_summary_when_following_progress_is_cited(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    output = _fixture("daily-active")
    output["telegram_text"] = output["telegram_text"].replace(
        "The validation direction became clearer and easier to build on this window.",
        "Alice moved the ERC-8004 validation direction into a clearer review phase.",
    )

    prepared = await DailyService(tmp_path, ai=FakeAi(output)).prepare(
        WINDOW, readiness=_ready(), evidence=_evidence(tmp_path)
    )

    assert prepared is not None
    assert "Alice moved the ERC-8004 validation direction" in prepared.telegram_text
