from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import tomllib
import types
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tawg_bot.telegram_webhook import MAX_BODY_BYTES, TelegramWebhookEnvelope

ROOT = Path(__file__).parents[2]
RAW_CHAT_ID = -100_123_456
RAW_SENDER_ID = 987_654_321
WEBHOOK_SECRET = "telegram_webhook_secret_32_chars_"
GITHUB_TOKEN = "github-production-token"


def _locked_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in (ROOT / "requirements-modal-deploy.lock").read_text(encoding="utf-8").splitlines():
        if "==" not in line or line[:1].isspace() or line.startswith("#"):
            continue
        name, version_with_suffix = line.split("==", maxsplit=1)
        packages[name.casefold().replace("_", "-")] = version_with_suffix.split()[0].rstrip("\\")
    return packages


class FakeResponse:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code


class FakeRequest:
    def __init__(
        self,
        body: bytes,
        *,
        secret: str | None = WEBHOOK_SECRET,
        chunk_size: int | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.headers = _CaseInsensitiveHeaders()
        if secret is not None:
            self.headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        self._body = body
        self._chunk_size = chunk_size or max(1, len(body))
        self._stream_error = stream_error
        self.chunks_read = 0

    async def stream(self) -> AsyncIterator[bytes]:
        if self._stream_error is not None:
            raise self._stream_error
        for offset in range(0, len(self._body), self._chunk_size):
            self.chunks_read += 1
            yield self._body[offset : offset + self._chunk_size]


class _CaseInsensitiveHeaders(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:
        normalized = key.casefold()
        return next(
            (value for name, value in self.items() if name.casefold() == normalized),
            default,
        )


class FakeCron:
    def __init__(self, expression: str) -> None:
        self.expression = expression


class FakeRetries:
    def __init__(self, *, max_retries: int) -> None:
        self.max_retries = max_retries


class FakeSecret:
    calls: ClassVar[list[tuple[str, tuple[str, ...]]]] = []

    def __init__(self, name: str, required_keys: tuple[str, ...]) -> None:
        self.name = name
        self.required_keys = required_keys

    @classmethod
    def from_name(cls, name: str, *, required_keys: list[str]) -> FakeSecret:
        required = tuple(required_keys)
        cls.calls.append((name, required))
        return cls(name, required)


class FakeImage:
    created: ClassVar[list[FakeImage]] = []

    def __init__(self, base: str, add_python: str | None) -> None:
        self.base = base
        self.add_python = add_python
        self.steps: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.created.append(self)

    @classmethod
    def from_registry(cls, base: str, *, add_python: str) -> FakeImage:
        return cls(base, add_python)

    def __getattr__(self, name: str) -> Callable[..., FakeImage]:
        def record(*args: Any, **kwargs: Any) -> FakeImage:
            self.steps.append((name, args, kwargs))
            return self

        return record


class FakeSpawn:
    def __init__(self, owner: FakeFunction) -> None:
        self.owner = owner

    def __call__(self, payload: dict[str, object] | None = None) -> object:
        if self.owner.spawn_error is not None:
            raise self.owner.spawn_error
        self.owner.sync_spawned.append(payload)
        self.owner.spawned.append(payload)
        return object()

    async def aio(self, payload: dict[str, object] | None = None) -> object:
        if self.owner.spawn_error is not None:
            raise self.owner.spawn_error
        self.owner.async_spawned.append(payload)
        self.owner.spawned.append(payload)
        return object()


class FakeFunction:
    def __init__(self, raw_f: Callable[..., Any], config: dict[str, Any]) -> None:
        self.raw_f = raw_f
        self.config = config
        self.spawned: list[dict[str, object] | None] = []
        self.sync_spawned: list[dict[str, object] | None] = []
        self.async_spawned: list[dict[str, object] | None] = []
        self.spawn_error: Exception | None = None
        self.spawn = FakeSpawn(self)


class FakeApp:
    created: ClassVar[list[FakeApp]] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.functions: list[FakeFunction] = []
        self.created.append(self)

    def function(self, **config: Any) -> Callable[[Callable[..., Any]], FakeFunction]:
        def decorate(raw_f: Callable[..., Any]) -> FakeFunction:
            function = FakeFunction(raw_f, config)
            self.functions.append(function)
            return function

        return decorate


def _fake_modal_module() -> types.ModuleType:
    module = types.ModuleType("modal")
    module.App = FakeApp
    module.Cron = FakeCron
    module.Image = FakeImage
    module.Retries = FakeRetries
    module.Secret = FakeSecret

    def fastapi_endpoint(**config: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(raw_f: Callable[..., Any]) -> Callable[..., Any]:
            raw_f.modal_web_config = config  # type: ignore[attr-defined]
            return raw_f

        return decorate

    module.fastapi_endpoint = fastapi_endpoint
    return module


def _fake_fastapi_module() -> types.ModuleType:
    module = types.ModuleType("fastapi")
    module.Request = FakeRequest
    module.Response = FakeResponse
    return module


@pytest.fixture
def modal_adapter(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    FakeApp.created.clear()
    FakeImage.created.clear()
    FakeSecret.calls.clear()
    monkeypatch.setitem(sys.modules, "modal", _fake_modal_module())
    monkeypatch.setitem(sys.modules, "fastapi", _fake_fastapi_module())
    sys.modules.pop("deploy.modal_app", None)
    module = importlib.import_module("deploy.modal_app")
    monkeypatch.setenv("TAWG_TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", str(RAW_CHAT_ID))
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "tawg_bot")
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    return module


def _telegram_body(*, text: str = "hello team") -> bytes:
    telegram_token_url = (
        "https://api.telegram.org/"
        "bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi/sendMessage"
    )
    return json.dumps(
        {
            "update_id": 701,
            "message": {
                "message_id": 42,
                "date": 1_788_000_000,
                "chat": {"id": RAW_CHAT_ID, "type": "supergroup"},
                "from": {
                    "id": RAW_SENDER_ID,
                    "username": "alice",
                    "first_name": "Alice",
                },
                "text": text,
                "private_note": "raw-body-fragment",
                "request_url": telegram_token_url,
            },
        }
    ).encode()


def test_all_core_modules_and_cli_parser_work_without_modal_in_a_clean_interpreter() -> None:
    script = """
import builtins
import importlib
import pkgutil

real_import = builtins.__import__

def block_modal(name, *args, **kwargs):
    if name == "modal" or name.startswith("modal."):
        raise ModuleNotFoundError("modal is intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = block_modal
package = importlib.import_module("tawg_bot")
for module_info in pkgutil.walk_packages(package.__path__, prefix="tawg_bot."):
    importlib.import_module(module_info.name)
cli = importlib.import_module("tawg_bot.cli")
assert cli._parser().parse_args(["tick", "--observe-only"]).command == "tick"
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_modal_dependencies_are_isolated_and_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["optional-dependencies"]["modal"] == [
        "modal==1.5.4",
        "fastapi==0.141.1",
    ]
    assert all(
        "modal" not in dependency and "fastapi" not in dependency
        for dependency in project["dependencies"]
    )
    assert all(
        "modal" not in dependency and "fastapi" not in dependency
        for dependency in project["optional-dependencies"]["dev"]
    )


def test_modal_deployment_lock_contains_every_direct_runtime_pin_with_hashes() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = {}
    for dependency in [
        *project["dependencies"],
        *project["optional-dependencies"]["modal"],
    ]:
        name, version = dependency.split("==", maxsplit=1)
        expected[name.casefold().replace("_", "-")] = version

    locked = _locked_packages()
    assert {name: locked.get(name) for name in expected} == expected
    lock_text = (ROOT / "requirements-modal-deploy.lock").read_text(encoding="utf-8")
    for name, version in expected.items():
        requirement = f"{name}=={version}"
        start = lock_text.casefold().index(requirement.casefold())
        next_requirement = lock_text.find("\n", start)
        assert "--hash=sha256:" in lock_text[next_requirement : next_requirement + 500]


def test_modal_lock_covers_core_third_party_imports_without_importing_host_packages() -> None:
    import_to_distribution = {
        "httpx": "httpx",
        "jsonschema": "jsonschema",
        "pydantic": "pydantic",
        "yaml": "pyyaml",
    }
    imported: set[str] = set()
    for source_path in (ROOT / "src/tawg_bot").glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module.partition(".")[0])

    required_distributions = {
        distribution
        for module, distribution in import_to_distribution.items()
        if module in imported
    }
    assert required_distributions <= _locked_packages().keys()


def test_adapter_separates_scheduled_trigger_from_single_writer_and_endpoint(
    modal_adapter: types.ModuleType,
) -> None:
    worker = modal_adapter.repository_worker
    maintenance = modal_adapter.scheduled_maintenance
    endpoint = modal_adapter.telegram_webhook

    assert "schedule" not in worker.config
    assert worker.config["max_containers"] == 1
    assert 1 <= worker.config["timeout"] <= 3_600
    assert 1 <= worker.config["retries"].max_retries <= 3
    assert maintenance.config["schedule"].expression == "*/5 * * * *"
    assert "max_containers" not in maintenance.config
    assert endpoint.config["timeout"] <= 30
    assert endpoint.raw_f.modal_web_config == {"method": "POST"}
    assert len(FakeApp.created) == 1
    assert FakeApp.created[0].functions == [worker, maintenance, endpoint]


def test_image_and_secret_cover_the_complete_runtime_without_source_secrets(
    modal_adapter: types.ModuleType,
) -> None:
    image = FakeImage.created[0]
    all_steps = repr(image.steps)
    secrets = {name: set(required_keys) for name, required_keys in FakeSecret.calls}

    assert image.base == (
        "node:22.23.1-bookworm@"
        "sha256:0557ac14e0d45d02ed563067b82856ca5e7aa3437fa28d98d4350ea9c3d9494a"
    )
    assert image.add_python == "3.12"
    assert not any(name in {"apt_install", "pip_install"} for name, _, _ in image.steps)
    assert not any(
        name == "run_commands" and "npm install" in repr(args)
        for name, args, _ in image.steps
    )
    assert (
        "add_local_file",
        ("requirements-modal-deploy.lock", "/opt/tawg/requirements-modal-deploy.lock"),
        {"copy": True},
    ) in image.steps
    assert (
        "add_local_file",
        ("deploy/claude-runtime/package.json", "/opt/tawg/claude-runtime/package.json"),
        {"copy": True},
    ) in image.steps
    assert (
        "add_local_file",
        (
            "deploy/claude-runtime/package-lock.json",
            "/opt/tawg/claude-runtime/package-lock.json",
        ),
        {"copy": True},
    ) in image.steps
    assert "python -m pip install --require-hashes" in all_steps
    assert "/opt/tawg/requirements-modal-deploy.lock" in all_steps
    assert "npm ci --omit=dev --ignore-scripts" in all_steps
    assert (
        "node /opt/tawg/claude-runtime/node_modules/"
        "@anthropic-ai/claude-code/install.cjs"
    ) in all_steps
    assert (
        "/opt/tawg/claude-runtime/node_modules/"
        "@anthropic-ai/claude-code/bin/claude.exe"
    ) in all_steps
    assert "test -x" in all_steps
    assert "src" in all_steps
    assert "config/privacy.yml" in all_steps
    assert secrets["tawg-webhook"] == {
        "TAWG_TELEGRAM_WEBHOOK_SECRET",
        "TAWG_TELEGRAM_CHAT_ID",
        "TAWG_TELEGRAM_BOT_USERNAME",
    }
    assert secrets["tawg-worker"] >= {
        "TELEGRAM_BOT_TOKEN",
        "TAWG_TELEGRAM_CHAT_ID",
        "TAWG_TELEGRAM_BOT_USERNAME",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "GITHUB_TOKEN",
        "TAWG_INVINOVERITAS_API_KEY",
    }
    assert secrets["tawg-github-announcements"] == {
        "TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID"
    }
    assert secrets["tawg-maintenance"] == {"TAWG_MODAL_MAINTENANCE_ENABLED"}
    assert "TAWG_TELEGRAM_WEBHOOK_SECRET" not in secrets["tawg-worker"]
    assert "GITHUB_TOKEN" not in secrets["tawg-webhook"]
    assert "TELEGRAM_BOT_TOKEN" not in secrets["tawg-webhook"]
    assert "ANTHROPIC_AUTH_TOKEN" not in secrets["tawg-webhook"]
    assert [secret.name for secret in modal_adapter.repository_worker.config["secrets"]] == [
        "tawg-worker",
        "tawg-github-announcements",
    ]
    assert [secret.name for secret in modal_adapter.telegram_webhook.config["secrets"]] == [
        "tawg-webhook"
    ]
    assert [secret.name for secret in modal_adapter.scheduled_maintenance.config["secrets"]] == [
        "tawg-maintenance"
    ]
    assert WEBHOOK_SECRET not in all_steps
    assert GITHUB_TOKEN not in all_steps
    env_steps = [step for step in image.steps if step[0] == "env"]
    assert len(env_steps) == 1
    assert env_steps[0][1][0] == {
        "PYTHONPATH": "/opt/tawg/src",
        "TAWG_REPOSITORY_PERSIST_MODE": "full",
        "TAWG_MODAL_BRANCH": "main",
        "TAWG_DEV_MODE": "false",
    }


def test_dev_mode_mounts_tawg_dev_secret_after_shared_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeApp.created.clear()
    FakeImage.created.clear()
    FakeSecret.calls.clear()
    monkeypatch.setitem(sys.modules, "modal", _fake_modal_module())
    monkeypatch.setitem(sys.modules, "fastapi", _fake_fastapi_module())
    monkeypatch.setenv("TAWG_DEV_MODE", "true")
    sys.modules.pop("deploy.modal_app", None)
    module = importlib.import_module("deploy.modal_app")

    secrets = {name: set(required_keys) for name, required_keys in FakeSecret.calls}
    assert secrets["tawg-dev"] == {
        "TELEGRAM_BOT_TOKEN",
        "TAWG_TELEGRAM_BOT_USERNAME",
        "TAWG_TELEGRAM_WEBHOOK_SECRET",
    }
    assert [secret.name for secret in module.repository_worker.config["secrets"]] == [
        "tawg-worker",
        "tawg-github-announcements",
        "tawg-dev",
    ]
    assert [secret.name for secret in module.telegram_webhook.config["secrets"]] == [
        "tawg-webhook",
        "tawg-dev",
    ]
    assert [secret.name for secret in module.scheduled_maintenance.config["secrets"]] == [
        "tawg-maintenance",
    ]
    image = FakeImage.created[0]
    env_step = next(step for step in image.steps if step[0] == "env")
    assert env_step[1][0]["TAWG_DEV_MODE"] == "true"


@pytest.mark.asyncio
async def test_endpoint_spawns_only_the_sanitized_json_envelope(
    modal_adapter: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = FakeRequest(_telegram_body(), chunk_size=31)

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 200
    assert modal_adapter.repository_worker.sync_spawned == []
    assert len(modal_adapter.repository_worker.async_spawned) == 1
    assert len(modal_adapter.repository_worker.spawned) == 1
    payload = modal_adapter.repository_worker.spawned[0]
    envelope = TelegramWebhookEnvelope.model_validate(payload)
    assert payload == envelope.model_dump(mode="json")
    combined_output = json.dumps(payload, sort_keys=True) + caplog.text
    assert str(RAW_CHAT_ID) not in combined_output
    assert str(RAW_SENDER_ID) not in combined_output
    assert WEBHOOK_SECRET not in combined_output
    assert GITHUB_TOKEN not in combined_output
    assert "raw-body-fragment" not in combined_output
    assert "api.telegram.org/bot" not in combined_output


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, "wrong-secret"])
async def test_endpoint_rejects_bad_auth_without_reading_or_spawning(
    modal_adapter: types.ModuleType,
    secret: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = FakeRequest(_telegram_body(text="must-never-be-read"), secret=secret)

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 403
    assert request.chunks_read == 0
    assert modal_adapter.repository_worker.spawned == []
    assert "must-never-be-read" not in caplog.text
    assert "wrong-secret" not in caplog.text


@pytest.mark.asyncio
async def test_endpoint_rejects_oversized_stream_before_reading_the_remainder(
    modal_adapter: types.ModuleType,
) -> None:
    request = FakeRequest(b"x" * (MAX_BODY_BYTES + 64 * 1024), chunk_size=64 * 1024)

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 400
    assert request.chunks_read == 5
    assert modal_adapter.repository_worker.spawned == []


class _OversizedChunk:
    def __init__(self) -> None:
        self.iterated = False

    def __len__(self) -> int:
        return MAX_BODY_BYTES + 1

    def __iter__(self) -> Any:
        self.iterated = True
        raise AssertionError("oversized chunk must not be extended into the body buffer")


class _SingleChunkRequest(FakeRequest):
    def __init__(self, chunk: _OversizedChunk) -> None:
        super().__init__(b"")
        self._chunk = chunk

    async def stream(self) -> AsyncIterator[bytes]:
        yield self._chunk  # type: ignore[misc]


@pytest.mark.asyncio
async def test_endpoint_rejects_one_oversized_chunk_before_extending_it(
    modal_adapter: types.ModuleType,
) -> None:
    chunk = _OversizedChunk()

    response = await modal_adapter.telegram_webhook.raw_f(_SingleChunkRequest(chunk))

    assert response.status_code == 400
    assert chunk.iterated is False
    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
async def test_endpoint_acknowledges_safe_ignore_and_rejects_malformed_input(
    modal_adapter: types.ModuleType,
) -> None:
    ignored = FakeRequest(json.dumps({"update_id": 800, "callback_query": {}}).encode())
    malformed = FakeRequest(b"not-json")

    ignored_response = await modal_adapter.telegram_webhook.raw_f(ignored)
    malformed_response = await modal_adapter.telegram_webhook.raw_f(malformed)

    assert ignored_response.status_code == 200
    assert malformed_response.status_code == 400
    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
async def test_endpoint_safely_rejects_request_stream_failure(
    modal_adapter: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_exception = f"disconnected {WEBHOOK_SECRET} {RAW_SENDER_ID}"
    request = FakeRequest(b"", stream_error=RuntimeError(sensitive_exception))

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 400
    assert sensitive_exception not in caplog.text
    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, "wrong-secret", "wrong-秘密"])
async def test_bad_auth_does_not_construct_normalizer_with_broken_config(
    modal_adapter: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    secret: str | None,
) -> None:
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "broken-chat-config")
    request = FakeRequest(_telegram_body(text="must-stay-unread"), secret=secret)

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 403
    assert request.chunks_read == 0
    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_secret",
    ["", "x", "x" * 31, "x" * 32 + "!", "x" * 31 + "秘"],
)
async def test_invalid_configured_secret_fails_before_body_or_normalizer_access(
    modal_adapter: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    configured_secret: str,
) -> None:
    monkeypatch.setenv("TAWG_TELEGRAM_WEBHOOK_SECRET", configured_secret)
    monkeypatch.setattr(
        modal_adapter,
        "_normalizer",
        lambda: pytest.fail("normalizer must not be constructed for invalid secret config"),
    )
    request = FakeRequest(_telegram_body(text="must-stay-unread"))

    response = await modal_adapter.telegram_webhook.raw_f(request)

    assert response.status_code == 503
    assert request.chunks_read == 0
    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
async def test_endpoint_returns_retryable_failure_when_spawn_is_not_accepted(
    modal_adapter: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    modal_adapter.repository_worker.spawn_error = RuntimeError(
        f"dispatch failed: {WEBHOOK_SECRET} {RAW_SENDER_ID}"
    )

    response = await modal_adapter.telegram_webhook.raw_f(FakeRequest(_telegram_body()))

    assert response.status_code == 503
    assert WEBHOOK_SECRET not in caplog.text
    assert str(RAW_SENDER_ID) not in caplog.text


class FakeRuntime:
    roots: ClassVar[list[Path]] = []
    envelopes: ClassVar[list[TelegramWebhookEnvelope]] = []
    maintenance_calls: ClassVar[list[tuple[datetime, bool]]] = []

    @classmethod
    def from_environment(cls, root: Path) -> FakeRuntime:
        cls.roots.append(root)
        return cls()

    async def ingest_webhook_envelope(
        self,
        envelope: TelegramWebhookEnvelope,
        *,
        now: datetime,
    ) -> None:
        assert now.tzinfo is UTC
        self.envelopes.append(envelope)

    async def maintenance_tick(self, now: datetime, *, observe_only: bool) -> None:
        self.maintenance_calls.append((now, observe_only))


class FakeRepositorySession:
    instances: ClassVar[list[FakeRepositorySession]] = []

    def __init__(self, *, remote: str, branch: str, merge_branch: str | None = None) -> None:
        self.remote = remote
        self.branch = branch
        self.merge_branch = merge_branch
        self.operation_ids: list[str] = []
        self.environments: list[dict[str, str]] = []
        self.instances.append(self)

    async def run(self, *, operation_id: str, operation: Callable[[Path], Any]) -> object:
        self.operation_ids.append(operation_id)
        self.environments.append(
            {
                key: value
                for key, value in os.environ.items()
                if key.startswith("GIT_CONFIG_") or key == "GITHUB_REF_NAME"
            }
        )
        return await operation(Path("/fresh/repository"))


@pytest.fixture
def fake_worker_dependencies(
    modal_adapter: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeRuntime.roots.clear()
    FakeRuntime.envelopes.clear()
    FakeRuntime.maintenance_calls.clear()
    FakeRepositorySession.instances.clear()
    for key in tuple(os.environ):
        if key.startswith("GIT_CONFIG_") or key == "GITHUB_REF_NAME":
            monkeypatch.delenv(key)
    monkeypatch.setattr(modal_adapter, "ProductionRuntime", FakeRuntime)
    monkeypatch.setattr(modal_adapter, "RepositorySession", FakeRepositorySession)


@pytest.mark.asyncio
async def test_worker_reconstructs_envelope_and_runs_ingestion_in_fresh_session(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
) -> None:
    del fake_worker_dependencies
    decision = modal_adapter._normalizer().process(WEBHOOK_SECRET, _telegram_body())
    assert decision.envelope is not None
    payload = TelegramWebhookEnvelope.model_validate_json(
        json.dumps(decision.envelope.model_dump(mode="json"))
    ).model_dump(mode="json")

    await modal_adapter.repository_worker.raw_f(payload)

    session = FakeRepositorySession.instances[0]
    assert session.remote == "https://github.com/trustless-ai/tawg-daily-contribution.git"
    assert session.branch == "main"
    assert session.operation_ids == ["webhook:701"]
    assert FakeRuntime.roots == [Path("/fresh/repository")]
    assert FakeRuntime.envelopes == [TelegramWebhookEnvelope.model_validate(payload)]
    assert FakeRuntime.maintenance_calls == []
    worker_environment = session.environments[0]
    assert worker_environment["GITHUB_REF_NAME"] == "main"
    assert worker_environment["GIT_CONFIG_COUNT"] == "3"
    assert worker_environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert GITHUB_TOKEN not in worker_environment["GIT_CONFIG_VALUE_0"]
    assert worker_environment["GIT_CONFIG_KEY_1"] == "user.name"
    assert worker_environment["GIT_CONFIG_VALUE_1"] == "TAWG Knowledge Bot"
    assert worker_environment["GIT_CONFIG_KEY_2"] == "user.email"
    assert worker_environment["GIT_CONFIG_VALUE_2"] == "tawg-knowledge-bot@users.noreply.github.com"
    assert not any(
        key.startswith("GIT_CONFIG_") or key == "GITHUB_REF_NAME" for key in os.environ
    )


@pytest.mark.asyncio
async def test_worker_restores_inherited_git_environment_after_session(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker_dependencies
    inherited = {
        "GIT_CONFIG_COUNT": "9",
        "GIT_CONFIG_KEY_0": "old-key",
        "GIT_CONFIG_VALUE_0": "old-value",
        "GIT_CONFIG_KEY_8": "unrelated-key",
        "GIT_CONFIG_VALUE_8": "unrelated-value",
        "GITHUB_REF_NAME": "feature/previous-work",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    await modal_adapter.repository_worker.raw_f()

    worker_environment = FakeRepositorySession.instances[0].environments[0]
    assert worker_environment["GITHUB_REF_NAME"] == "main"
    assert worker_environment["GIT_CONFIG_COUNT"] == "3"
    assert worker_environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "GIT_CONFIG_KEY_8" not in worker_environment
    assert "GIT_CONFIG_VALUE_8" not in worker_environment
    assert {key: os.environ[key] for key in inherited} == inherited


@pytest.mark.asyncio
async def test_scheduled_no_argument_worker_runs_maintenance_in_the_same_boundary(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del fake_worker_dependencies

    await modal_adapter.repository_worker.raw_f()

    session = FakeRepositorySession.instances[0]
    assert session.remote == "https://github.com/trustless-ai/tawg-daily-contribution.git"
    assert session.branch == "main"
    assert session.operation_ids[0].startswith("maintenance:")
    assert FakeRuntime.envelopes == []
    assert len(FakeRuntime.maintenance_calls) == 1
    now, observe_only = FakeRuntime.maintenance_calls[0]
    assert now.tzinfo is UTC
    assert observe_only is False
    assert capsys.readouterr().out.splitlines() == [
        "tawg_event=worker_started operation=maintenance",
        "tawg_event=worker_completed operation=maintenance",
    ]


@pytest.mark.asyncio
async def test_worker_failure_log_is_safe_and_re_raises(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del fake_worker_dependencies

    async def fail_run(self: object, **kwargs: object) -> object:
        del self, kwargs
        raise RuntimeError("sensitive backend failure")

    monkeypatch.setattr(modal_adapter.RepositorySession, "run", fail_run)

    with pytest.raises(RuntimeError, match="sensitive backend failure"):
        await modal_adapter.repository_worker.raw_f()

    rendered = capsys.readouterr().out
    assert rendered.splitlines() == [
        "tawg_event=worker_started operation=maintenance",
        "tawg_event=worker_failed operation=maintenance",
    ]
    assert "sensitive backend failure" not in rendered


@pytest.mark.asyncio
async def test_malformed_webhook_payload_emits_safe_worker_failure_log(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del fake_worker_dependencies

    with pytest.raises(ValueError):
        await modal_adapter.repository_worker.raw_f({"update_id": "not-an-integer"})

    rendered = capsys.readouterr().out
    assert rendered.splitlines() == [
        "tawg_event=worker_started operation=webhook",
        "tawg_event=worker_failed operation=webhook",
    ]
    assert "not-an-integer" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [None, "", "false", "TRUE", "1", " true "])
async def test_scheduled_maintenance_is_a_hard_noop_unless_exactly_true(
    modal_adapter: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    enabled: str | None,
) -> None:
    if enabled is None:
        monkeypatch.delenv("TAWG_MODAL_MAINTENANCE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TAWG_MODAL_MAINTENANCE_ENABLED", enabled)

    await modal_adapter.scheduled_maintenance.raw_f()

    assert modal_adapter.repository_worker.spawned == []


@pytest.mark.asyncio
async def test_scheduled_maintenance_spawns_shared_worker_only_when_exactly_true(
    modal_adapter: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAWG_MODAL_MAINTENANCE_ENABLED", "true")

    await modal_adapter.scheduled_maintenance.raw_f()

    assert modal_adapter.repository_worker.sync_spawned == []
    assert modal_adapter.repository_worker.async_spawned == [None]
    assert modal_adapter.repository_worker.spawned == [None]


@pytest.mark.asyncio
async def test_dev_worker_merges_main_and_skips_maintenance_tick(
    modal_adapter: types.ModuleType,
    fake_worker_dependencies: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker_dependencies
    monkeypatch.setattr(modal_adapter, "_BRANCH", "dev")

    await modal_adapter.repository_worker.raw_f()

    session = FakeRepositorySession.instances[0]
    assert session.branch == "dev"
    assert session.merge_branch == "main"
    assert FakeRuntime.envelopes == []
    assert FakeRuntime.maintenance_calls == []


def test_claude_runtime_lock_is_exact_and_integrity_pinned() -> None:
    package = json.loads(
        (ROOT / "deploy/claude-runtime/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "deploy/claude-runtime/package-lock.json").read_text(encoding="utf-8")
    )

    assert package["dependencies"] == {"@anthropic-ai/claude-code": "2.1.240"}
    locked = lock["packages"]["node_modules/@anthropic-ai/claude-code"]
    assert locked["version"] == "2.1.240"
    assert locked["integrity"].startswith("sha512-")
