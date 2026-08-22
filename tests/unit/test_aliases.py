from pathlib import Path

import pytest

from tawg_bot.aliases import AliasRegistry, AmbiguousAlias, InvalidAliasScope


def test_exact_source_handle_and_normalized_display_name_lookup() -> None:
    registry = AliasRegistry()
    person_id = registry.resolve_public_handle(
        source="telegram", public_handle="@Alice_ETH", display_name="Alice Zhang"
    )
    registry.add_public_handle(person_id, source="github", public_handle="alice-eth")

    assert registry.lookup_public_handle("telegram", "alice_eth") == person_id
    assert registry.lookup_public_handle("github", "ALICE-ETH") == person_id
    assert registry.lookup_display_name("  ALICE   ZHANG ") == person_id


def test_display_name_collision_is_not_silently_merged() -> None:
    registry = AliasRegistry()
    first = registry.resolve_telegram_export(transient_key="one", display_name="Alice")
    second = registry.resolve_telegram_export(transient_key="two", display_name="Alice")

    assert (first, second) == ("alice", "alice-2")
    with pytest.raises(AmbiguousAlias):
        registry.lookup_display_name("alice")


def test_explicit_merge_combines_aliases_and_removes_secondary() -> None:
    registry = AliasRegistry()
    primary = registry.resolve_public_handle(
        source="telegram", public_handle="alice_eth", display_name="Alice"
    )
    secondary = registry.resolve_public_handle(
        source="github", public_handle="alice-z", display_name="Alice Zhang"
    )

    registry.merge(primary, secondary)

    assert registry.lookup_public_handle("github", "alice-z") == primary
    assert secondary not in registry.people


def test_registry_rejects_cross_tawg_person_namespaces(tmp_path: Path) -> None:
    aliases = tmp_path / "aliases.yml"
    aliases.write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople:\n  other-tawg:alice: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidAliasScope):
        AliasRegistry.from_yaml(aliases)
