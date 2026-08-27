from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tawg_bot.cli import _parser, main
from tawg_bot.telegram_webhook import TelegramWebhookEnvelope

NOW = datetime(2026, 8, 24, 1, 17, tzinfo=UTC)
SECRET_TEXT = "private sanitized webhook text"


def envelope_payload() -> dict[str, object]:
    return {
        "schema_version": "tawg.telegram-webhook-envelope.v1",
        "update_id": 42,
        "source_id": "tg:tawg:500",
        "message_id": 500,
        "timestamp": int(NOW.timestamp()),
        "edited": False,
        "edited_timestamp": None,
        "text": SECRET_TEXT,
        "public_username": "alice_tawg",
        "display_name": "Alice",
        "reply_to_message_id": None,
        "message_thread_id": 77,
        "entities": [],
        "attachments": [],
        "triggers_reply": False,
        "integrity_digest": "a" * 64,
    }


class Runtime:
    def __init__(self) -> None:
        self.ticks: list[tuple[datetime, bool]] = []
        self.webhooks: list[tuple[TelegramWebhookEnvelope, datetime]] = []
        self.maintenance: list[tuple[datetime, bool]] = []

    async def tick(self, now: datetime, *, observe_only: bool) -> None:
        self.ticks.append((now, observe_only))

    async def ingest_webhook_envelope(
        self, envelope: TelegramWebhookEnvelope, *, now: datetime
    ) -> object:
        self.webhooks.append((envelope, now))
        return SimpleNamespace(received=1, persisted=1, replayed=0, jobs_created=0)

    async def maintenance_tick(self, now: datetime, *, observe_only: bool) -> None:
        self.maintenance.append((now, observe_only))


def test_cli_ingests_one_confined_envelope_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "envelope.json"
    source.write_text(json.dumps(envelope_payload()), encoding="utf-8")
    runtime = Runtime()

    assert (
        main(
            [
                "ingest-webhook-envelope",
                "--input",
                "envelope.json",
                "--now",
                "2026-08-24T01:17:00Z",
            ],
            runtime=runtime,  # type: ignore[arg-type]
        )
        == 0
    )

    assert runtime.webhooks[0][0].update_id == 42
    assert runtime.webhooks[0][1] == NOW
    captured = capsys.readouterr()
    assert SECRET_TEXT not in captured.out
    assert SECRET_TEXT not in captured.err


def test_cli_accepts_the_explicit_stdin_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(envelope_payload())))
    runtime = Runtime()

    assert (
        main(
            ["ingest-webhook-envelope", "--input", "-"],
            runtime=runtime,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        == 0
    )

    assert runtime.webhooks[0][0].message_id == 500
    captured = capsys.readouterr()
    assert SECRET_TEXT not in captured.out
    assert SECRET_TEXT not in captured.err


@pytest.mark.parametrize("invalid_input", [[envelope_payload()], "not-json"])
def test_cli_rejects_non_envelope_input_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_input: object,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "invalid.json"
    source.write_text(
        json.dumps(invalid_input) if invalid_input != "not-json" else SECRET_TEXT,
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        main(
            ["ingest-webhook-envelope", "--input", "invalid.json"],
            runtime=Runtime(),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )

    captured = capsys.readouterr()
    assert SECRET_TEXT not in captured.out
    assert SECRET_TEXT not in captured.err


def test_cli_rejects_an_envelope_path_outside_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    source = tmp_path / "outside.json"
    source.write_text(json.dumps(envelope_payload()), encoding="utf-8")
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit):
        main(
            ["ingest-webhook-envelope", "--input", str(source)],
            runtime=Runtime(),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )


def test_cli_exposes_unambiguous_polling_and_maintenance_commands() -> None:
    parser = _parser()

    assert parser.parse_args(["tick"]).command == "tick"
    assert parser.parse_args(["maintenance-tick"]).command == "maintenance-tick"
    with pytest.raises(SystemExit):
        parser.parse_args(["maintenance-tick", "--input", "envelope.json"])


def test_cli_dispatches_action_equivalent_observe_only_polling_tick() -> None:
    runtime = Runtime()

    assert (
        main(
            ["tick", "--now", "2026-08-24T01:17:00Z", "--observe-only"],
            runtime=runtime,  # type: ignore[arg-type]
        )
        == 0
    )

    assert runtime.ticks == [(NOW, True)]
    assert runtime.maintenance == []


def test_cli_dispatches_maintenance_without_polling() -> None:
    runtime = Runtime()

    assert (
        main(
            ["maintenance-tick", "--now", "2026-08-24T01:17:00Z", "--observe-only"],
            runtime=runtime,  # type: ignore[arg-type]
        )
        == 0
    )

    assert runtime.maintenance == [(NOW, True)]
