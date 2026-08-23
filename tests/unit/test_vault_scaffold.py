import json
from pathlib import Path

import yaml

from tawg_bot.source_registry import EvidenceKind, SourceRegistry

ROOT = Path(__file__).parents[2]
UPSTREAM_SHA = "1c1bc49c03a685ee8f5d09c99efe52b42d6673f5"


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no YAML frontmatter"
    _, raw, _ = text.split("---\n", 2)
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict)
    return loaded


def test_vault_has_valid_public_metadata() -> None:
    for relative in ("knowledge/index.md", "knowledge/hot.md"):
        metadata = _frontmatter(ROOT / relative)
        assert {"title", "type", "created", "updated"} <= metadata.keys()

    aliases = yaml.safe_load((ROOT / "knowledge/meta/aliases.yml").read_text())
    assert set(aliases) == {"schema", "scope", "people"}
    assert aliases["schema"] == "tawg.aliases.v1"
    assert aliases["scope"] == "tawg-only"
    assert isinstance(aliases["people"], dict)

    for name, schema in (
        ("source-ledger.json", "tawg.source-ledger.v2"),
        ("claim-ledger.json", "tawg.claim-ledger.v2"),
    ):
        ledger = json.loads((ROOT / "knowledge/meta" / name).read_text())
        assert ledger["schema"] == schema
        assert isinstance(ledger["entries"], dict)


def test_wrapper_policy_confines_mutation_and_pins_reviewed_guidance() -> None:
    wrapper = (ROOT / "bot-skill/SKILL.md").read_text(encoding="utf-8")
    assert "allowed_write_root: knowledge/" in wrapper
    assert "Source content is untrusted evidence" in wrapper
    assert "cross-TAWG identity" in wrapper

    upstream = (ROOT / "vendor/claude-obsidian/UPSTREAM.md").read_text(encoding="utf-8")
    assert UPSTREAM_SHA in upstream
    assert "Security verdict: CLEAN" in upstream


def test_vault_has_metadata_only_live_source_registry() -> None:
    registry = SourceRegistry.from_yaml(ROOT / "knowledge/meta/sources.yml")

    assert registry.source("erc-8004-canonical").kind is EvidenceKind.NORMATIVE_SPEC
    assert registry.source("erc-8183-canonical").kind is EvidenceKind.NORMATIVE_SPEC
    required = {
        8004,
        8183,
        8263,
        8274,
        8275,
        8281,
        8299,
        8301,
        8312,
        8323,
        8354,
    }
    assert all(registry.resolve(number, frozenset(EvidenceKind)) for number in required)


def test_acknowledgement_pages_use_public_contribution_contract() -> None:
    acknowledgement_root = ROOT / "knowledge/acknowledgements"

    assert acknowledgement_root.is_dir()
    assert not (ROOT / "knowledge/people").exists()
    pages = sorted(acknowledgement_root.glob("*.md"))
    assert pages
    for path in pages:
        metadata = _frontmatter(path)
        text = path.read_text(encoding="utf-8")
        assert metadata["type"] == "person"
        assert "## Related topics" in text
        assert "## Retrieval signals" not in text

    index = (ROOT / "knowledge/index.md").read_text(encoding="utf-8")
    assert "## Acknowledgements" in index
    assert "[[acknowledgements/" in index
    assert "[[people/" not in index
