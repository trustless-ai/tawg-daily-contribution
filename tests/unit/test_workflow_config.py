from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def workflow() -> dict:
    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        key: [item for item in value if item[0] != "tag:yaml.org,2002:bool"]
        for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return yaml.load((ROOT / ".github/workflows/tawg-knowledge.yml").read_text(), Loader=Loader)


def test_workflow_is_single_non_overlapping_five_minute_writer() -> None:
    value = workflow()

    assert value["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    inputs = value["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"observe_only", "daily_dry_run"}
    assert value["permissions"] == {"contents": "write"}
    assert value["concurrency"] == {
        "group": "tawg-knowledge-writer",
        "cancel-in-progress": "false",
    }


def test_workflow_pins_runtime_and_hardens_claude_environment() -> None:
    value = workflow()
    job = value["jobs"]["knowledge-bot"]
    rendered = (ROOT / ".github/workflows/tawg-knowledge.yml").read_text()

    setup = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python")
    )
    assert setup["with"]["python-version"] == "3.12"
    assert "@anthropic-ai/claude-code@2.1.240" in rendered
    assert job["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert job["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert job["env"]["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert "echo $" not in rendered
    assert "git-auto-commit" not in rendered


def test_checkpoint_script_restricts_paths_and_rejects_non_fast_forward_push() -> None:
    script = (ROOT / "scripts/commit_operation.sh").read_text()

    for allowed in ("data/", "knowledge/"):
        assert allowed in script
    for forbidden in ("contracts/", ".github/", "config/", "scripts/"):
        assert forbidden in script
    assert "git push" in script
    assert "--force" not in script
    assert "git pull" not in script
    assert "tawg_bot.cli vault-lint" in script
    assert "git add -- data knowledge .vault-meta" not in script
