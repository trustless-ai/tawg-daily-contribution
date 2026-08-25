from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tawg_bot.source_registry import (
    EvidenceAuthority,
    EvidenceKind,
    RegistryRejected,
    SourceObservation,
    SourceRegistry,
)


def _source(
    source_key: str,
    topic: str,
    kind: str,
    authority: str,
    url: str,
) -> dict[str, object]:
    host = url.split("/", 3)[2].split("@")[-1]
    path = "/" + url.split("/", 3)[3].split("?", 1)[0].split("#", 1)[0]
    return {
        "source_key": source_key,
        "topics": [topic],
        "kind": kind,
        "authority": authority,
        "canonical_url": url,
        "immutable_url": None,
        "fetch_policy": {
            "policy": "public-text",
            "allowed_hosts": [host],
            "allowed_path_prefixes": [path],
            "max_bytes": 250_000,
            "mime_types": ["text/html", "text/plain", "text/markdown"],
        },
        "last_observed": None,
        "status": "active",
    }


def _registry_payload() -> dict[str, object]:
    return {
        "schema": "tawg.sources.v2",
        "sources": [
            _source(
                "erc-8004-canonical",
                "erc-8004",
                "normative_spec",
                "canonical",
                "https://eips.ethereum.org/EIPS/eip-8004",
            ),
            _source(
                "agent-ercs-8004-implementation",
                "erc-8004",
                "implementation",
                "official_org",
                "https://github.com/trustless-ai/agent-ercs/blob/main/ERCs/ERC-8004.md",
            ),
            _source(
                "agent-ercs-8004-tests",
                "erc-8004",
                "test",
                "official_org",
                "https://github.com/trustless-ai/agent-ercs/tree/main/tests/erc-8004",
            ),
            _source(
                "magicians-8004",
                "erc-8004",
                "discussion",
                "community",
                "https://ethereum-magicians.org/t/erc-8004-trustless-agents/25098",
            ),
            _source(
                "erc-8183-canonical",
                "erc-8183",
                "normative_spec",
                "canonical",
                "https://eips.ethereum.org/EIPS/eip-8183",
            ),
        ],
    }


def _write_registry(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "sources.yml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_registry_resolves_by_evidence_kind_then_authority(tmp_path: Path) -> None:
    registry = SourceRegistry.from_yaml(_write_registry(tmp_path, _registry_payload()))

    sources = registry.resolve(8004, frozenset(EvidenceKind))

    assert [source.source_key for source in sources] == [
        "erc-8004-canonical",
        "agent-ercs-8004-implementation",
        "agent-ercs-8004-tests",
        "magicians-8004",
    ]
    assert registry.source("erc-8004-canonical").authority is EvidenceAuthority.CANONICAL


@pytest.mark.parametrize(
    "url",
    [
        "http://eips.ethereum.org/EIPS/eip-8004",
        "https://user:pass@eips.ethereum.org/EIPS/eip-8004",
        "https://example.invalid/current",
        "https://eips.ethereum.org/EIPS/eip-8004?raw=1",
        "https://eips.ethereum.org/EIPS/eip-8004#fragment",
    ],
)
def test_registry_rejects_unsafe_source_urls(tmp_path: Path, url: str) -> None:
    payload = _registry_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    first = sources[0]
    assert isinstance(first, dict)
    first["canonical_url"] = url

    with pytest.raises(RegistryRejected):
        SourceRegistry.from_yaml(_write_registry(tmp_path, payload))


def test_registry_rejects_external_body_fields(tmp_path: Path) -> None:
    payload = _registry_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    first = sources[0]
    assert isinstance(first, dict)
    first["content"] = "copied external specification"

    with pytest.raises(RegistryRejected, match="invalid source registry"):
        SourceRegistry.from_yaml(_write_registry(tmp_path, payload))


def test_registry_requires_one_canonical_source_for_8004_and_8183(tmp_path: Path) -> None:
    payload = _registry_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    payload["sources"] = [
        source
        for source in sources
        if isinstance(source, dict) and source["source_key"] != "erc-8183-canonical"
    ]

    with pytest.raises(RegistryRejected, match="erc-8183"):
        SourceRegistry.from_yaml(_write_registry(tmp_path, payload))


def test_registry_allows_other_ercs_to_expose_a_normative_gap(tmp_path: Path) -> None:
    payload = _registry_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources.append(
        _source(
            "magicians-8203",
            "erc-8203",
            "discussion",
            "community",
            "https://ethereum-magicians.org/t/erc-8203-discussion/28365",
        )
    )

    registry = SourceRegistry.from_yaml(_write_registry(tmp_path, payload))

    assert [source.source_key for source in registry.resolve(8203, frozenset(EvidenceKind))] == [
        "magicians-8203"
    ]


def test_registry_rejects_duplicate_keys_and_duplicate_canonical_sources(tmp_path: Path) -> None:
    duplicate_key = _registry_payload()
    duplicate_sources = duplicate_key["sources"]
    assert isinstance(duplicate_sources, list)
    duplicate_sources.append(duplicate_sources[0])
    with pytest.raises(RegistryRejected, match="duplicate source key"):
        SourceRegistry.from_yaml(_write_registry(tmp_path, duplicate_key))

    duplicate_canonical = _registry_payload()
    canonical_sources = duplicate_canonical["sources"]
    assert isinstance(canonical_sources, list)
    canonical_sources.append(
        _source(
            "erc-8004-second-canonical",
            "erc-8004",
            "normative_spec",
            "canonical",
            "https://eips.ethereum.org/EIPS/eip-8004-copy",
        )
    )
    with pytest.raises(RegistryRejected, match="multiple canonical sources"):
        SourceRegistry.from_yaml(_write_registry(tmp_path, duplicate_canonical))


def test_registry_renders_current_observation_without_a_body(tmp_path: Path) -> None:
    registry = SourceRegistry.from_yaml(_write_registry(tmp_path, _registry_payload()))
    observation = SourceObservation(
        checked_at=datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC),
        version="abc123",
        content_sha256="a" * 64,
        byte_count=1234,
    )

    rendered = registry.render_with_observations({"erc-8004-canonical": observation})
    round_trip_path = tmp_path / "round-trip.yml"
    round_trip_path.write_text(rendered, encoding="utf-8")
    updated = SourceRegistry.from_yaml(round_trip_path)

    assert updated.source("erc-8004-canonical").last_observed == observation
    assert "copied external specification" not in rendered


def test_registry_only_marks_ercs_with_stale_sources_due_for_recheck(tmp_path: Path) -> None:
    registry = SourceRegistry.from_yaml(_write_registry(tmp_path, _registry_payload()))
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    fresh = SourceObservation(
        checked_at=now - timedelta(hours=23),
        version="fresh",
        content_sha256="a" * 64,
        byte_count=123,
    )
    stale = fresh.model_copy(update={"checked_at": now - timedelta(hours=25)})
    observations = {
        source.source_key: (stale if "erc-8183" in source.topics else fresh)
        for erc in registry.erc_numbers()
        for source in registry.resolve(erc, frozenset(EvidenceKind))
    }
    updated_path = tmp_path / "updated.yml"
    updated_path.write_text(
        registry.render_with_observations(observations),
        encoding="utf-8",
    )

    updated = SourceRegistry.from_yaml(updated_path)

    assert updated.due_erc_numbers(now, max_age=timedelta(hours=24)) == (8183,)
