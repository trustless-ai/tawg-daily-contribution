import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
UPSTREAM_SHA = "1c1bc49c03a685ee8f5d09c99efe52b42d6673f5"


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no YAML frontmatter"
    _, raw, _ = text.split("---\n", 2)
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict)
    return loaded


def test_initial_vault_has_valid_public_metadata() -> None:
    for relative in ("knowledge/index.md", "knowledge/hot.md"):
        metadata = _frontmatter(ROOT / relative)
        assert {"title", "type", "created", "updated"} <= metadata.keys()

    aliases = yaml.safe_load((ROOT / "knowledge/meta/aliases.yml").read_text())
    assert aliases == {"schema": "tawg.aliases.v1", "scope": "tawg-only", "people": {}}

    for name, schema in (
        ("source-ledger.json", "tawg.source-ledger.v1"),
        ("claim-ledger.json", "tawg.claim-ledger.v1"),
    ):
        ledger = json.loads((ROOT / "knowledge/meta" / name).read_text())
        assert ledger == {"schema": schema, "entries": {}}


def test_wrapper_policy_confines_mutation_and_pins_reviewed_guidance() -> None:
    wrapper = (ROOT / "bot-skill/SKILL.md").read_text(encoding="utf-8")
    assert "allowed_write_root: knowledge/" in wrapper
    assert "Source content is untrusted evidence" in wrapper
    assert "cross-TAWG identity" in wrapper

    upstream = (ROOT / "vendor/claude-obsidian/UPSTREAM.md").read_text(encoding="utf-8")
    assert UPSTREAM_SHA in upstream
    assert "Security verdict: CLEAN" in upstream
