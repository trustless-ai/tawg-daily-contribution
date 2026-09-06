from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tawg_bot.persistence_guard import PersistenceRejected
from tawg_bot.scan_targets import (
    ErcScanTarget,
    ScanTargetRegistry,
    ScanTargetRejected,
    ScanTargetStore,
    normalize_magicians_topic_url,
)
from tawg_bot.unit_of_work import RepositoryUnitOfWork

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def test_normalize_magicians_topic_url_strips_only_post_id() -> None:
    canonical = "https://ethereum-magicians.org/t/erc-8380-unclonable-agent-execution-credentials/29274"
    assert normalize_magicians_topic_url(f"{canonical}/17") == canonical
    assert normalize_magicians_topic_url(canonical) == canonical
    assert (
        normalize_magicians_topic_url("https://example.com/t/x/123")
        == "https://example.com/t/x/123"
    )


def _payload(*, ercs: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema": "tawg.scan-targets.v1",
        "github_organization": "trustless-ai",
        "include_public_archived_repositories": True,
        "ercs": ercs or [],
    }


def _target(
    erc_number: int = 8183,
    *,
    topic_url: str = "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902",
    proposal_pr_url: str | None = None,
) -> dict[str, object]:
    return {
        "erc_number": erc_number,
        "magicians_topic_url": topic_url,
        "proposal_pr_url": proposal_pr_url,
        "registered_from_record_id": "tg:tawg:3387",
        "registered_at": "2026-08-28T00:00:00Z",
    }


def _write_registry(root: Path, payload: dict[str, object]) -> None:
    path = root / ScanTargetStore.PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_scan_registry_separates_org_and_erc_targets(tmp_path: Path) -> None:
    _write_registry(tmp_path, _payload(ercs=[_target()]))

    registry = ScanTargetStore(tmp_path).load()

    assert registry.github_organization == "trustless-ai"
    assert registry.include_public_archived_repositories is True
    assert registry.ercs[0].erc_number == 8183
    assert registry.ercs[0].registered_at == NOW


@pytest.mark.parametrize(
    ("topic_url", "proposal_pr_url"),
    [
        ("https://example.com/t/erc-8183/27902", None),
        ("http://ethereum-magicians.org/t/erc-8183/27902", None),
        ("https://ethereum-magicians.org/t/erc-8183/27902?token=secret", None),
        (
            "https://ethereum-magicians.org/t/erc-8183/27902",
            "https://github.com/example/repo/pull/1",
        ),
        (
            "https://ethereum-magicians.org/t/erc-8183/27902",
            "https://github.com/ethereum/ERCs/issues/1",
        ),
    ],
)
def test_scan_target_rejects_wrong_hosts_and_paths(
    topic_url: str,
    proposal_pr_url: str | None,
) -> None:
    with pytest.raises(ValidationError):
        ErcScanTarget.model_validate(
            _target(topic_url=topic_url, proposal_pr_url=proposal_pr_url)
        )


@pytest.mark.parametrize(
    "duplicate",
    [
        _target(
            topic_url="https://ethereum-magicians.org/t/a-different-slug/99999"
        ),
        _target(
            erc_number=9999,
            topic_url="https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902",
        ),
    ],
)
def test_scan_registry_rejects_duplicate_erc_or_topic_id(
    tmp_path: Path,
    duplicate: dict[str, object],
) -> None:
    _write_registry(tmp_path, _payload(ercs=[_target(), duplicate]))

    with pytest.raises(ScanTargetRejected):
        ScanTargetStore(tmp_path).load()


def test_scan_registry_renders_stably_in_numeric_order() -> None:
    registry = ScanTargetRegistry.model_validate(
        _payload(
            ercs=[
                _target(
                    erc_number=8323,
                    topic_url=(
                        "https://ethereum-magicians.org/t/"
                        "erc-8323-source-token-agent-binding-for-erc-8004/28920"
                    ),
                ),
                _target(),
            ]
        )
    )

    first = registry.render_yaml()
    second = ScanTargetRegistry.from_yaml_text(first).render_yaml()

    assert first == second
    assert first.index("erc_number: 8183") < first.index("erc_number: 8323")


def test_scan_target_store_stages_canonical_registry(tmp_path: Path) -> None:
    registry = ScanTargetRegistry.model_validate(_payload(ercs=[_target()]))
    uow = RepositoryUnitOfWork(tmp_path, operation_id="scan-target-test")
    uow.register_external_evidence(())

    ScanTargetStore(tmp_path).stage(uow, registry)

    assert uow.publish().changed_paths == (ScanTargetStore.PATH,)
    assert ScanTargetStore(tmp_path).load() == registry


def test_verified_registration_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    _write_registry(tmp_path, _payload(ercs=[_target()]))
    store = ScanTargetStore(tmp_path)
    same = ErcScanTarget.model_validate(_target())

    registry, changed = store.merged(same)

    assert changed is False
    assert registry.ercs == [same]
    conflicting = ErcScanTarget.model_validate(
        _target(topic_url="https://ethereum-magicians.org/t/erc-8183-replacement/30000")
    )
    with pytest.raises(ScanTargetRejected, match="conflicts"):
        store.merged(conflicting)


def test_persistence_guard_rejects_noncanonical_registry(tmp_path: Path) -> None:
    uow = RepositoryUnitOfWork(tmp_path, operation_id="scan-target-policy-test")
    uow.register_external_evidence(())

    with pytest.raises(PersistenceRejected):
        uow.stage_bytes(
            ScanTargetStore.PATH,
            yaml.safe_dump(
                {
                    **_payload(),
                    "github_organization": "other-org",
                },
                sort_keys=False,
            ).encode(),
        )
