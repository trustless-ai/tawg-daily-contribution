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


def test_mention_label_refuses_a_display_name_that_already_contains_a_handle() -> None:
    registry = AliasRegistry()
    person_id = registry.resolve_public_handle(
        source="telegram",
        public_handle="thepoktopus",
        display_name="Jinx | @thepoktopus",
    )
    assert (
        registry.telegram_mention_label(
            source="telegram",
            author_person_id=person_id,
        )
        is None
    )


def test_mention_label_resolves_an_explicitly_linked_github_identity() -> None:
    registry = AliasRegistry()
    person_id = registry.resolve_public_handle(
        source="telegram",
        public_handle="gobross",
        display_name="Merlini",
    )
    registry.add_public_handle(person_id, source="github", public_handle="tmerlini")

    assert registry.telegram_mention_label(
        source="github",
        author_person_id="tmerlini",
    ) == "Merlini (@gobross)"


def test_registry_rejects_a_malformed_telegram_username_suffix() -> None:
    registry = AliasRegistry()

    with pytest.raises(InvalidAliasScope):
        registry.resolve_public_handle(
            source="telegram",
            public_handle="alice_tg.foo",
            display_name="Alice",
        )


def test_live_telegram_identity_falls_back_when_username_is_not_actionable() -> None:
    registry = AliasRegistry()

    person_id = registry.resolve_telegram_live(
        public_username="bob",
        display_name="Bob",
    )

    assert person_id == "bob"
    assert registry.people[person_id]["handles"].get("telegram", []) == []


@pytest.mark.parametrize("public_username", [None, "bob"])
def test_live_identity_without_actionable_username_does_not_reuse_handled_person(
    public_username: str | None,
) -> None:
    registry = AliasRegistry()
    handled_person = registry.resolve_public_handle(
        source="telegram",
        public_handle="alice_tg",
        display_name="Alice",
    )

    fallback_person = registry.resolve_telegram_live(
        public_username=public_username,
        display_name="Alice",
    )

    assert fallback_person != handled_person
    assert registry.telegram_mention_label(
        source="telegram",
        author_person_id=fallback_person,
    ) is None


@pytest.mark.parametrize(
    "display_name",
    [
        "Alice\nAdmin",
        "Alice [tg:tawg:1]",
        "Alice\u2028Admin",
        "Alice\u202eAdmin",
        "Alice\u200bAdmin",
    ],
)
def test_mention_label_refuses_unsafe_display_name_syntax(display_name: str) -> None:
    registry = AliasRegistry()
    person_id = registry.resolve_public_handle(
        source="telegram",
        public_handle="alice_tg",
        display_name=display_name,
    )

    assert (
        registry.telegram_mention_label(
            source="telegram",
            author_person_id=person_id,
        )
        is None
    )


def test_registry_rejects_cross_tawg_person_namespaces(tmp_path: Path) -> None:
    aliases = tmp_path / "aliases.yml"
    aliases.write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople:\n  other-tawg:alice: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidAliasScope):
        AliasRegistry.from_yaml(aliases)
