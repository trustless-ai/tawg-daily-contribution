import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ACTION_PINS = {
    "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
}


def workflow() -> dict:
    return load_workflow("tawg-knowledge.yml")


def load_workflow(name: str) -> dict:
    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        key: [item for item in value if item[0] != "tag:yaml.org,2002:bool"]
        for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=Loader
    )


def test_workflow_is_single_non_overlapping_five_minute_writer() -> None:
    value = workflow()

    assert value["on"]["schedule"] == [{"cron": "2-59/5 * * * *"}]
    inputs = value["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"operation"}
    assert inputs["operation"] == {
        "description": "Choose an operation; each option states whether it can send",
        "type": "choice",
        "options": [
            "Run normally — process due work and send",
            "Preview latest Daily — generate only, do not send",
            "Check only — inspect without Telegram delivery",
            "Poll now — process backlog and due Daily, then send",
        ],
        "default": "Run normally — process due work and send",
    }
    assert value["permissions"] == {"contents": "write"}
    assert value["concurrency"] == {
        "group": "tawg-knowledge-writer",
        "cancel-in-progress": "false",
    }
    assert set(value["jobs"]) == {"knowledge-bot"}
    assert value["jobs"]["knowledge-bot"]["if"] == "github.ref == 'refs/heads/main'"


def test_workflow_pins_runtime_and_hardens_claude_environment() -> None:
    value = workflow()
    job = value["jobs"]["knowledge-bot"]
    rendered = (ROOT / ".github/workflows/tawg-knowledge.yml").read_text()

    assert "env" not in value
    setup = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/setup-python")
    )
    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] == "false"
    assert setup["with"]["python-version"] == "3.12"
    assert "env" not in job
    install_claude = next(
        step for step in job["steps"] if step["name"] == "Install locked Claude runtime"
    )
    assert install_claude["working-directory"] == "deploy/claude-runtime"
    assert install_claude["run"] == "npm ci --omit=dev --ignore-scripts"
    assert "npm install --global" not in rendered
    assert "echo $" not in rendered
    assert "git-auto-commit" not in rendered


def test_locked_claude_install_is_ignored_by_repository_checkpoints() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--quiet",
            "--no-index",
            "deploy/claude-runtime/node_modules/@anthropic-ai/claude-code/cli-wrapper.cjs",
        ],
        cwd=ROOT,
        check=False,
    )

    assert ignored.returncode == 0


def test_workflow_selects_safe_core_commands_for_authoritative_runtime_modes() -> None:
    value = workflow()
    job = value["jobs"]["knowledge-bot"]
    rendered = (ROOT / ".github/workflows/tawg-knowledge.yml").read_text()

    operation_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Run bot with ordered repository checkpoints"
    )
    run_bot = operation_step["run"]
    operation_env = operation_step["env"]
    assert set(operation_env) == {
        "PYTHONUNBUFFERED",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
        "GITHUB_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "TAWG_TELEGRAM_CHAT_ID",
        "TAWG_TELEGRAM_BOT_USERNAME",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "TAWG_DELIVERY_ENABLED",
        "TAWG_RUNTIME_MODE",
        "MANUAL_OPERATION",
    }
    assert operation_env["GITHUB_TOKEN"] == "${{ github.token }}"
    assert operation_env["TELEGRAM_BOT_TOKEN"] == "${{ secrets.TELEGRAM_BOT_TOKEN }}"
    assert operation_env["ANTHROPIC_AUTH_TOKEN"] == "${{ secrets.ANTHROPIC_AUTH_TOKEN }}"
    assert operation_env["TAWG_RUNTIME_MODE"] == "${{ vars.TAWG_RUNTIME_MODE }}"
    assert operation_env["MANUAL_OPERATION"] == (
        "${{ inputs.operation || 'Run normally — process due work and send' }}"
    )
    for step in job["steps"]:
        if step is operation_step:
            continue
        assert "${{ secrets." not in str(step)
        assert "${{ github.token }}" not in str(step)
    assert 'authoritative_mode="${TAWG_RUNTIME_MODE:-poll}"' in run_bot
    assert 'manual_operation="$MANUAL_OPERATION"' in run_bot
    assert "${{ inputs." not in run_bot
    assert "Run normally — process due work and send" in run_bot
    assert "Preview latest Daily — generate only, do not send" in run_bot
    assert "Check only — inspect without Telegram delivery" in run_bot
    assert "Poll now — process backlog and due Daily, then send" in run_bot
    assert '[[ "$authoritative_mode" == "webhook" ]]' in run_bot
    assert (
        "Manual backlog polling is disabled while the authoritative runtime mode is webhook"
        in run_bot
    )
    assert "daily-dry-run --window-end" in run_bot
    assert "operation=(tick)" in run_bot
    assert "operation=(maintenance-tick)" in run_bot
    assert "operation=(maintenance-tick --observe-only)" in run_bot
    assert "operation=(tick --observe-only)" in run_bot
    assert 'python -m tawg_bot.cli "${operation[@]}"' in run_bot
    assert '"$TAWG_DELIVERY_ENABLED" != "true"' in run_bot
    assert "operation+=(--observe-only)" in run_bot
    assert (
        'export PATH="$GITHUB_WORKSPACE/deploy/claude-runtime/node_modules/.bin:$PATH"'
        in run_bot
    )
    assert 'export GIT_CONFIG_COUNT="1"' in run_bot
    assert 'export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"' in run_bot
    assert 'export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $git_authorization"' in run_bot
    assert 'git_credential="x-access-token:$GITHUB_TOKEN"' in run_bot
    assert "printf '%s' \"$git_credential\" | base64" in run_bot
    assert 'base64 <<<"x-access-token:$GITHUB_TOKEN"' not in run_bot
    assert 'printf "x-access-token:%s" "$GITHUB_TOKEN"' not in run_bot
    assert "git config" not in run_bot
    assert "trap clear_runtime_credentials EXIT" in run_bot
    assert "unset git_credential git_authorization" in run_bot
    assert "unset GITHUB_TOKEN GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0" in run_bot
    for unsafe_command in ("set -x", "set -o xtrace", "printenv"):
        assert unsafe_command not in run_bot
    assert all(not line.strip().startswith("env") for line in run_bot.splitlines())
    assert "getUpdates" not in rendered


def run_manual_operation(
    tmp_path: Path,
    *,
    operation: str,
    authoritative_mode: str,
    delivery_enabled: bool,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    value = workflow()
    job = value["jobs"]["knowledge-bot"]
    operation_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Run bot with ordered repository checkpoints"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    fake_python = bin_dir / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$COMMAND_LOG\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_date = bin_dir / "date"
    fake_date.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == '-u +%H' ]]; then printf '23\\n'; exit 0; fi\n"
        "printf '2026-08-27T23:00:00Z\\n'\n",
        encoding="utf-8",
    )
    fake_date.chmod(0o755)
    environment = {
        "COMMAND_LOG": str(command_log),
        "GITHUB_TOKEN": "test-token",
        "GITHUB_WORKSPACE": str(ROOT),
        "LC_ALL": "C",
        "MANUAL_OPERATION": operation,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "TAWG_DELIVERY_ENABLED": "true" if delivery_enabled else "false",
        "TAWG_RUNTIME_MODE": authoritative_mode,
    }
    completed = subprocess.run(
        ["bash", "-c", operation_step["run"]],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    commands = command_log.read_text().splitlines() if command_log.exists() else []
    return completed, commands


@pytest.mark.parametrize(
    ("operation", "authoritative_mode", "delivery_enabled", "expected_command"),
    [
        ("Run normally — process due work and send", "poll", True, "-m tawg_bot.cli tick"),
        (
            "Run normally — process due work and send",
            "poll",
            False,
            "-m tawg_bot.cli tick --observe-only",
        ),
        (
            "Run normally — process due work and send",
            "webhook",
            True,
            "-m tawg_bot.cli maintenance-tick",
        ),
        (
            "Run normally — process due work and send",
            "webhook",
            False,
            "-m tawg_bot.cli maintenance-tick --observe-only",
        ),
        (
            "Run normally — process due work and send",
            "observe",
            True,
            "-m tawg_bot.cli tick --observe-only",
        ),
        (
            "Check only — inspect without Telegram delivery",
            "poll",
            True,
            "-m tawg_bot.cli tick --observe-only",
        ),
        (
            "Check only — inspect without Telegram delivery",
            "webhook",
            True,
            "-m tawg_bot.cli maintenance-tick --observe-only",
        ),
        (
            "Poll now — process backlog and due Daily, then send",
            "poll",
            True,
            "-m tawg_bot.cli tick",
        ),
        (
            "Poll now — process backlog and due Daily, then send",
            "poll",
            False,
            "-m tawg_bot.cli tick --observe-only",
        ),
    ],
)
def test_manual_operation_routes_to_expected_command(
    tmp_path: Path,
    operation: str,
    authoritative_mode: str,
    delivery_enabled: bool,
    expected_command: str,
) -> None:
    completed, commands = run_manual_operation(
        tmp_path,
        operation=operation,
        authoritative_mode=authoritative_mode,
        delivery_enabled=delivery_enabled,
    )

    assert completed.returncode == 0, completed.stderr
    assert commands == [expected_command]


def test_manual_daily_preview_only_generates_without_delivery(tmp_path: Path) -> None:
    completed, commands = run_manual_operation(
        tmp_path,
        operation="Preview latest Daily — generate only, do not send",
        authoritative_mode="poll",
        delivery_enabled=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert len(commands) == 1
    assert commands[0].startswith(
        "-m tawg_bot.cli daily-dry-run --window-end "
    )


def test_manual_backlog_poll_is_rejected_in_webhook_mode(tmp_path: Path) -> None:
    completed, commands = run_manual_operation(
        tmp_path,
        operation="Poll now — process backlog and due Daily, then send",
        authoritative_mode="webhook",
        delivery_enabled=True,
    )

    assert completed.returncode == 2
    assert commands == []
    assert "Manual backlog polling is disabled" in completed.stderr


def test_manual_operation_rejects_untrusted_input_without_shell_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "injected"
    completed, commands = run_manual_operation(
        tmp_path,
        operation=f'Run normally"; touch {marker}; #',
        authoritative_mode="poll",
        delivery_enabled=True,
    )

    assert completed.returncode == 2
    assert commands == []
    assert not marker.exists()


def test_workflows_pin_third_party_actions_to_reviewed_commits() -> None:
    expected_versions = {
        "actions/checkout": "v4.2.2",
        "actions/setup-python": "v5.6.0",
        "actions/setup-node": "v4",
    }

    for name in ("tawg-knowledge.yml", "modal-deploy.yml"):
        value = load_workflow(name)
        rendered = (ROOT / ".github/workflows" / name).read_text()
        for job in value["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if uses is None:
                    continue
                action, commit = uses.rsplit("@", 1)
                assert re.fullmatch(r"[0-9a-f]{40}", commit)
                assert commit == ACTION_PINS[action]
                assert f"uses: {action}@{commit} # {expected_versions[action]}" in rendered


def test_modal_deploy_workflow_verifies_before_pinned_least_privilege_deploy() -> None:
    value = load_workflow("modal-deploy.yml")
    job = value["jobs"]["deploy"]
    rendered = (ROOT / ".github/workflows/modal-deploy.yml").read_text()

    assert set(value["on"]) == {"push", "workflow_dispatch"}
    assert value["on"]["push"] == {
        "branches": ["main"],
        "paths": [
            ".github/workflows/modal-deploy.yml",
            "config/privacy.yml",
            "deploy/**",
            "pyproject.toml",
            "requirements-modal-deploy.in",
            "requirements-modal-deploy.lock",
            "src/**",
        ],
    }
    assert value["permissions"] == {"contents": "read"}
    assert value["concurrency"] == {
        "group": "tawg-modal-deploy",
        "cancel-in-progress": "false",
    }
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "tawg-production"
    setup = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/setup-python")
    )
    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert setup["with"]["python-version"] == "3.12"
    assert "python -m pip install --require-hashes -r requirements-dev.lock" in rendered
    assert "python -m pip install --no-deps ." in rendered
    assert "python -m pip install --require-hashes -r requirements-modal-deploy.lock" in rendered
    assert "python -m pip install modal==1.5.4" not in rendered
    assert "modal deploy deploy/modal_app.py" in rendered
    deploy_index = rendered.index("modal deploy deploy/modal_app.py")
    revision_check = next(
        step["run"] for step in job["steps"] if step["name"] == "Verify checked out revision"
    )
    assert 'actual_sha="$(git rev-parse HEAD)"' in revision_check
    assert '[[ "$actual_sha" != "$GITHUB_SHA" ]]' in revision_check
    assert rendered.index("git rev-parse HEAD") < rendered.index(
        "python -m ruff check src tests deploy"
    )
    for verification_command in (
        "python -m ruff check src tests deploy",
        "python -m mypy src/tawg_bot",
        "python -m pytest -q",
        "python -m tawg_bot.cli vault-lint",
    ):
        assert verification_command in rendered
        assert rendered.index(verification_command) < deploy_index
    deploy_step = next(step for step in job["steps"] if "modal deploy" in step.get("run", ""))
    assert deploy_step["env"] == {
        "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
        "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
    }
    run_bodies = [step["run"] for step in job["steps"] if "run" in step]
    assert all("${{ secrets." not in run for run in run_bodies)
    for unsafe_command in ("set -x", "set -o xtrace", "printenv", "env |", "echo $"):
        assert all(unsafe_command not in run for run in run_bodies)
    assert all(
        all(not line.strip().startswith("env") for line in run.splitlines())
        for run in run_bodies
    )
    deployment_lock = (ROOT / "requirements-modal-deploy.lock").read_text()
    deployment_input = (ROOT / "requirements-modal-deploy.in").read_text()
    assert "-c requirements-dev.lock" in deployment_input
    assert "modal==1.5.4" in deployment_lock
    assert "fastapi==0.141.1" in deployment_lock
    assert "pydantic==2.11.7" in deployment_lock
    assert "--hash=sha256:" in deployment_lock
    assert "setWebhook" not in rendered


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


def run_checkpoint_with_push_failure(
    tmp_path: Path,
    *,
    push_output: str,
) -> subprocess.CompletedProcess[bytes]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    temporary_dir = tmp_path / "tmp"
    temporary_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == diff && \"$*\" == *--quiet* ]]; then exit 1; fi\n"
        "if [[ \"$1\" == push ]]; then\n"
        "  [[ \"${LC_ALL:-}\" == C ]] || exit 91\n"
        "  [[ \"$*\" == *--porcelain* ]] || exit 92\n"
        "  printf '%s\\n' \"$FAKE_PUSH_OUTPUT\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    environment = {
        **os.environ,
        "FAKE_PUSH_OUTPUT": push_output,
        "GITHUB_REF_NAME": "main",
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(temporary_dir),
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/commit_operation.sh"), "test:conflict"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert list(temporary_dir.iterdir()) == []
    return completed


def test_checkpoint_script_receipt_only_requires_bot_id(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/commit_operation.sh"), "test:op"],
        cwd=tmp_path,
        env={
            **os.environ,
            "TAWG_REPOSITORY_PERSIST_MODE": "receipt-only",
            "GITHUB_REF_NAME": "dev",
        },
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 6


def test_checkpoint_script_none_mode_is_noop(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/commit_operation.sh"), "test:op"],
        cwd=tmp_path,
        env={
            **os.environ,
            "TAWG_REPOSITORY_PERSIST_MODE": "none",
            "GITHUB_REF_NAME": "dev",
        },
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize("reason", ["non-fast-forward", "fetch first"])
def test_checkpoint_script_classifies_non_fast_forward_push_as_conflict(
    tmp_path: Path, reason: str
) -> None:
    completed = run_checkpoint_with_push_failure(
        tmp_path,
        push_output=(
            f"!\tHEAD:refs/heads/main\t[rejected] ({reason})\n"
            "secret https://credential@example.invalid/repository.git"
        ),
    )

    assert completed.returncode == 75
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_checkpoint_script_keeps_generic_push_failure_non_conflict_and_private(
    tmp_path: Path,
) -> None:
    completed = run_checkpoint_with_push_failure(
        tmp_path,
        push_output="fatal: Authentication failed for 'https://secret@example.invalid/repo'",
    )

    assert completed.returncode not in {0, 75}
    assert completed.stdout == b""
    assert completed.stderr == b""
