from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tawg_bot.live_evidence import EvidenceObservation
from tawg_bot.persistence_guard import (
    PersistenceGuard,
    PersistenceProvenance,
    PersistenceRejected,
)

EXTERNAL_BODY = (
    "EXTERNAL-CANARY-9f63: the normative source body is transient and must never be "
    "copied into repository state, metadata, queues, or diagnostic output."
)
QUERY_CANARY = "access_token=query-canary-74f1"


def test_persistent_evidence_metadata_cannot_represent_a_body() -> None:
    with pytest.raises(ValidationError):
        EvidenceObservation.model_validate(
            {
                "source_key": "erc-8004-canonical",
                "observed_at": "2026-08-23T00:00:00Z",
                "version": "fixture-v1",
                "content_sha256": "0" * 64,
                "source_byte_count": len(EXTERNAL_BODY.encode()),
                "text": EXTERNAL_BODY,
            }
        )


@pytest.mark.parametrize(
    ("relative_path", "value", "provenance"),
    [
        (
            "data/state/source-cursors.json",
            {"safe_error_code": EXTERNAL_BODY},
            PersistenceProvenance.OPERATIONAL_STATE,
        ),
        (
            "knowledge/meta/source-ledger.json",
            {"schema": "tawg.source-ledger.v2", "entries": {"body": EXTERNAL_BODY}},
            PersistenceProvenance.SOURCE_METADATA,
        ),
        (
            "data/state/pending-knowledge-refresh.json",
            [{"safe_error_code": EXTERNAL_BODY}],
            PersistenceProvenance.OPERATIONAL_STATE,
        ),
        (
            "data/github/2026/08/records.jsonl",
            {"text_original": EXTERNAL_BODY},
            PersistenceProvenance.EXTERNAL_EVIDENCE,
        ),
    ],
)
def test_rejects_external_body_canaries_from_persistent_outputs(
    relative_path: str,
    value: object,
    provenance: PersistenceProvenance,
) -> None:
    payload = (json.dumps(value) + "\n").encode()
    guard = PersistenceGuard.from_external_texts((EXTERNAL_BODY,))

    with pytest.raises(PersistenceRejected, match="persistence policy rejection") as caught:
        guard.inspect_staged(
            {relative_path: payload},
            {relative_path: provenance},
        )

    assert EXTERNAL_BODY not in str(caught.value)


@pytest.mark.parametrize(
    "external_body",
    [
        "0123456789abcdefghij\n" * 8,
        ('quoted "body" with a \\ slash and newline\n' * 5),
        "short-body",
    ],
)
def test_rejects_json_escaped_and_short_complete_bodies(external_body: str) -> None:
    relative_path = "data/state/source-cursors.json"
    guard = PersistenceGuard.from_external_texts((external_body,))

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        guard.inspect_staged(
            {relative_path: json.dumps({"safe_error_code": external_body}).encode()},
            {relative_path: PersistenceProvenance.OPERATIONAL_STATE},
        )


@pytest.mark.parametrize("external_body", ["2026-08-23", "true", "8183"])
def test_rejects_yaml_plain_scalars_that_look_typed(external_body: str) -> None:
    relative_path = "knowledge/meta/sources.yml"
    guard = PersistenceGuard.from_external_texts((external_body,))

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        guard.inspect_staged(
            {relative_path: f"version: {external_body}\n".encode()},
            {relative_path: PersistenceProvenance.SOURCE_METADATA},
        )


def test_rejects_short_body_embedded_in_a_forbidden_field() -> None:
    relative_path = "data/state/source-cursors.json"
    guard = PersistenceGuard.from_external_texts(("short-body",))

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        guard.inspect_staged(
            {relative_path: json.dumps({"safe_error_code": "error: short-body"}).encode()},
            {relative_path: PersistenceProvenance.OPERATIONAL_STATE},
        )


def test_allows_only_a_short_supported_quote_in_generated_output() -> None:
    guard = PersistenceGuard.from_external_texts((EXTERNAL_BODY,))
    short_quote = EXTERNAL_BODY.split(": ", 1)[1][:42]

    guard.inspect_staged(
        {
            "knowledge/ercs/erc-8004.md": f"# ERC-8004\n\n{short_quote}\n".encode(),
            "data/state/prepared-daily.json": json.dumps({"telegram_text": short_quote}).encode(),
        },
        {
            "knowledge/ercs/erc-8004.md": PersistenceProvenance.GENERATED_KNOWLEDGE,
            "data/state/prepared-daily.json": PersistenceProvenance.PREPARED_TELEGRAM,
        },
    )


@pytest.mark.parametrize(
    ("relative_path", "provenance"),
    [
        (
            "knowledge/ercs/erc-8004.md",
            PersistenceProvenance.GENERATED_KNOWLEDGE,
        ),
        (
            "data/state/prepared-daily.json",
            PersistenceProvenance.PREPARED_TELEGRAM,
        ),
    ],
)
def test_rejects_long_copied_excerpt_even_from_generated_output(
    relative_path: str, provenance: PersistenceProvenance
) -> None:
    guard = PersistenceGuard.from_external_texts((EXTERNAL_BODY,))
    copied_excerpt = EXTERNAL_BODY[20:125]

    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        guard.inspect_staged(
            {relative_path: f"generated prefix: {copied_excerpt}".encode()},
            {relative_path: provenance},
        )


def test_pending_job_quote_allowance_is_limited_to_prepared_reply_text() -> None:
    external = EXTERNAL_BODY
    short_quote = external[30:72]
    relative_path = "data/state/pending-bot-jobs.json"
    guard = PersistenceGuard.from_external_texts((external,))

    guard.inspect_staged(
        {relative_path: json.dumps([{"prepared_reply_text": short_quote}]).encode()},
        {relative_path: PersistenceProvenance.PREPARED_TELEGRAM},
    )
    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        guard.inspect_staged(
            {relative_path: json.dumps([{"safe_error_code": short_quote}]).encode()},
            {relative_path: PersistenceProvenance.PREPARED_TELEGRAM},
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "knowledge/raw.txt",
        "knowledge/ercs/erc-8004.json",
        "knowledge/file.exe",
        "knowledge/meta/nested/escape.md",
    ],
)
def test_rejects_non_markdown_generated_artifact_types(relative_path: str) -> None:
    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        PersistenceGuard().inspect_staged(
            {relative_path: b"generated"},
            {relative_path: PersistenceProvenance.GENERATED_KNOWLEDGE},
        )


def test_rejects_path_provenance_mismatch_without_echoing_payload() -> None:
    guard = PersistenceGuard()

    with pytest.raises(PersistenceRejected, match="persistence policy rejection") as caught:
        guard.inspect_staged(
            {"data/state/secret.json": b'{"value":"do-not-echo"}'},
            {
                "data/state/secret.json": PersistenceProvenance.GENERATED_KNOWLEDGE,
            },
        )

    assert "do-not-echo" not in str(caught.value)


def test_all_non_telegram_writes_require_explicit_evidence_binding(
    tmp_path: Path,
) -> None:
    from tawg_bot.unit_of_work import RepositoryUnitOfWork

    unbound = RepositoryUnitOfWork(tmp_path, operation_id="unbound-generated")
    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        unbound.stage_bytes("knowledge/summary.md", b"# Generated summary\n")
    with pytest.raises(PersistenceRejected, match="persistence policy rejection"):
        unbound.stage_json("data/state/source-cursors.json", {"telegram_offset": 1})

    bound = RepositoryUnitOfWork(tmp_path, operation_id="bound-generated")
    bound.register_external_evidence(())
    bound.stage_bytes("knowledge/summary.md", b"# Generated summary\n")
    assert bound.publish().changed_paths == ("knowledge/summary.md",)


def test_rejection_never_writes_or_logs_the_canary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tawg_bot.unit_of_work import RepositoryUnitOfWork

    external = f"{EXTERNAL_BODY} https://example.test/source?{QUERY_CANARY}"
    uow = RepositoryUnitOfWork(tmp_path, operation_id="canary-state")
    uow.register_external_evidence((external,))

    with pytest.raises(PersistenceRejected):
        uow.stage_json("data/state/knowledge-gaps.json", [{"safe_error_code": external}])

    assert not (tmp_path / "data/state/knowledge-gaps.json").exists()
    assert not uow.transaction_dir.exists()
    captured = capsys.readouterr()
    assert EXTERNAL_BODY not in captured.out
    assert EXTERNAL_BODY not in captured.err
    assert QUERY_CANARY not in captured.out
    assert QUERY_CANARY not in captured.err
