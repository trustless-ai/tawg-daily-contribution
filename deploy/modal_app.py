"""Thin Modal deployment adapter for webhook and scheduled repository work."""

from __future__ import annotations

import base64
import hmac
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import modal
from fastapi import Request, Response

from tawg_bot.bot_identity import configured_bot_id
from tawg_bot.privacy import PrivacyFilter
from tawg_bot.repository_session import RepositorySession
from tawg_bot.runtime import ProductionRuntime
from tawg_bot.telegram_webhook import (
    MAX_BODY_BYTES,
    TelegramWebhookConfig,
    TelegramWebhookDisposition,
    TelegramWebhookEnvelope,
    TelegramWebhookNormalizer,
    is_valid_telegram_webhook_secret,
)

_APP_NAME = os.environ.get("TAWG_MODAL_APP_NAME", "tawg-production")
_REMOTE = "https://github.com/trustless-ai/tawg-daily-contribution.git"
_BRANCH = os.environ.get("TAWG_MODAL_BRANCH", "main")
_REPOSITORY_PERSIST_MODE = os.environ.get(
    "TAWG_REPOSITORY_PERSIST_MODE",
    "none" if os.environ.get("TAWG_REPOSITORY_PERSIST_ENABLED", "true") == "false" else "full",
)
_DEV_MODE = os.environ.get("TAWG_DEV_MODE", "false") == "true"
_RUNTIME_ROOT = Path("/opt/tawg")
_PRIVACY_CONFIG = _RUNTIME_ROOT / "config/privacy.yml"
_LOCAL_PRIVACY_CONFIG = Path(__file__).parents[1] / "config/privacy.yml"
_WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"
_ENDPOINT_TIMEOUT_SECONDS = 10
_WORKER_TIMEOUT_SECONDS = 3_600
_WORKER_RETRIES = 2
_IMAGE_REFERENCE = (
    "node:22.23.1-bookworm@"
    "sha256:0557ac14e0d45d02ed563067b82856ca5e7aa3437fa28d98d4350ea9c3d9494a"
)
_REQUIREMENTS_LOCK = _RUNTIME_ROOT / "requirements-modal-deploy.lock"
_CLAUDE_RUNTIME = _RUNTIME_ROOT / "claude-runtime"
_WEBHOOK_SECRET_KEYS = [
    "TAWG_TELEGRAM_WEBHOOK_SECRET",
    "TAWG_TELEGRAM_CHAT_ID",
    "TAWG_TELEGRAM_BOT_USERNAME",
]
_WORKER_SECRET_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TAWG_TELEGRAM_CHAT_ID",
    "TAWG_TELEGRAM_BOT_USERNAME",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "GITHUB_TOKEN",
    "TAWG_INVINOVERITAS_API_KEY",
]
_GITHUB_ANNOUNCEMENT_SECRET_KEYS = [
    "TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID",
]
_MAINTENANCE_SECRET_KEYS = ["TAWG_MODAL_MAINTENANCE_ENABLED"]
_DEV_SECRET_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TAWG_TELEGRAM_BOT_USERNAME",
    "TAWG_TELEGRAM_WEBHOOK_SECRET",
]

image = (
    modal.Image.from_registry(_IMAGE_REFERENCE, add_python="3.12")
    .add_local_file(
        "requirements-modal-deploy.lock",
        str(_REQUIREMENTS_LOCK),
        copy=True,
    )
    .run_commands(
        f"python -m pip install --require-hashes -r {_REQUIREMENTS_LOCK}",
    )
    .add_local_file(
        "deploy/claude-runtime/package.json",
        str(_CLAUDE_RUNTIME / "package.json"),
        copy=True,
    )
    .add_local_file(
        "deploy/claude-runtime/package-lock.json",
        str(_CLAUDE_RUNTIME / "package-lock.json"),
        copy=True,
    )
    .run_commands(
        f"npm ci --omit=dev --ignore-scripts --prefix {_CLAUDE_RUNTIME}",
        "node "
        f"{_CLAUDE_RUNTIME}/node_modules/@anthropic-ai/claude-code/install.cjs",
        "test -x "
        f"{_CLAUDE_RUNTIME}/node_modules/@anthropic-ai/claude-code/bin/claude.exe "
        "&& ln -s "
        f"{_CLAUDE_RUNTIME}/node_modules/@anthropic-ai/claude-code/bin/claude.exe "
        "/usr/local/bin/claude",
    )
    .add_local_dir("src", str(_RUNTIME_ROOT / "src"), copy=True)
    .add_local_file("config/privacy.yml", str(_PRIVACY_CONFIG), copy=True)
    .env(
        {
            "PYTHONPATH": str(_RUNTIME_ROOT / "src"),
            "TAWG_REPOSITORY_PERSIST_MODE": _REPOSITORY_PERSIST_MODE,
            "TAWG_MODAL_BRANCH": _BRANCH,
            "TAWG_DEV_MODE": "true" if _DEV_MODE else "false",
        }
    )
    .workdir(str(_RUNTIME_ROOT))
)
webhook_secret = modal.Secret.from_name(
    "tawg-webhook",
    required_keys=_WEBHOOK_SECRET_KEYS,
)
worker_secret = modal.Secret.from_name(
    "tawg-worker",
    required_keys=_WORKER_SECRET_KEYS,
)
github_announcement_secret = modal.Secret.from_name(
    "tawg-github-announcements",
    required_keys=_GITHUB_ANNOUNCEMENT_SECRET_KEYS,
)
maintenance_secret = modal.Secret.from_name(
    "tawg-maintenance",
    required_keys=_MAINTENANCE_SECRET_KEYS,
)
dev_secret = (
    modal.Secret.from_name("tawg-dev", required_keys=_DEV_SECRET_KEYS)
    if _DEV_MODE
    else None
)
worker_secrets = [worker_secret, github_announcement_secret]
webhook_secrets = [webhook_secret]
if dev_secret is not None:
    worker_secrets.append(dev_secret)
    webhook_secrets.append(dev_secret)
app = modal.App(_APP_NAME)


def _normalizer() -> TelegramWebhookNormalizer:
    privacy_config = _PRIVACY_CONFIG if _PRIVACY_CONFIG.is_file() else _LOCAL_PRIVACY_CONFIG
    return TelegramWebhookNormalizer(
        config=TelegramWebhookConfig(
            secret_token=os.environ["TAWG_TELEGRAM_WEBHOOK_SECRET"],
            chat_id=int(os.environ["TAWG_TELEGRAM_CHAT_ID"]),
            group_slug="tawg",
            bot_username=os.environ["TAWG_TELEGRAM_BOT_USERNAME"],
        ),
        privacy=PrivacyFilter.from_yaml(privacy_config),
    )


@contextmanager
def _repository_environment() -> Iterator[None]:
    managed_keys = {
        key
        for key in os.environ
        if key.startswith("GIT_CONFIG_") or key in {"GITHUB_REF_NAME", "TAWG_BOT_ID"}
    }
    previous = {key: os.environ[key] for key in managed_keys}
    token = os.environ["GITHUB_TOKEN"]
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    bot_id = configured_bot_id()
    try:
        for key in managed_keys:
            del os.environ[key]
        os.environ["GIT_CONFIG_COUNT"] = "3"
        os.environ["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        os.environ["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
        os.environ["GIT_CONFIG_KEY_1"] = "user.name"
        os.environ["GIT_CONFIG_VALUE_1"] = "TAWG Knowledge Bot"
        os.environ["GIT_CONFIG_KEY_2"] = "user.email"
        os.environ["GIT_CONFIG_VALUE_2"] = "tawg-knowledge-bot@users.noreply.github.com"
        os.environ["GITHUB_REF_NAME"] = _BRANCH
        if bot_id is not None:
            os.environ["TAWG_BOT_ID"] = str(bot_id)
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith("GIT_CONFIG_") or key in {"GITHUB_REF_NAME", "TAWG_BOT_ID"}:
                del os.environ[key]
        os.environ.update(previous)


async def _bounded_body(stream: AsyncIterator[bytes]) -> bytes | None:
    body = bytearray()
    async for chunk in stream:
        if len(chunk) > MAX_BODY_BYTES - len(body):
            return None
        body.extend(chunk)
    return bytes(body)


@app.function(
    image=image,
    secrets=worker_secrets,
    max_containers=1,
    timeout=_WORKER_TIMEOUT_SECONDS,
    retries=modal.Retries(max_retries=_WORKER_RETRIES),
)
async def repository_worker(envelope_payload: dict[str, object] | None = None) -> None:
    """Run one webhook ingestion or maintenance repository session."""
    now = datetime.now(UTC)
    operation_kind = "webhook" if envelope_payload is not None else "maintenance"
    print(f"tawg_event=worker_started operation={operation_kind}", flush=True)
    try:
        envelope = (
            TelegramWebhookEnvelope.model_validate(envelope_payload)
            if envelope_payload is not None
            else None
        )
        operation_id = (
            f"webhook:{envelope.update_id}"
            if envelope is not None
            else f"maintenance:{int(now.timestamp())}"
        )

        async def run_runtime(root: Path) -> None:
            runtime = ProductionRuntime.from_environment(root)
            if envelope is not None:
                await runtime.ingest_webhook_envelope(envelope, now=now)
            elif _BRANCH == "main":
                await runtime.maintenance_tick(now, observe_only=False)
            # dev maintenance is sync-only; the merge already happened above

        merge_branch = None if _BRANCH == "main" else "main"
        with _repository_environment():
            await RepositorySession(
                remote=_REMOTE,
                branch=_BRANCH,
                merge_branch=merge_branch,
            ).run(
                operation_id=operation_id,
                operation=run_runtime,
            )
    except Exception:
        print(f"tawg_event=worker_failed operation={operation_kind}", flush=True)
        raise
    print(f"tawg_event=worker_completed operation={operation_kind}", flush=True)


@app.function(
    image=image,
    secrets=[maintenance_secret],
    schedule=modal.Cron("*/5 * * * *"),
    timeout=_ENDPOINT_TIMEOUT_SECONDS,
)
async def scheduled_maintenance() -> None:
    """Dispatch maintenance only after an explicit production enablement."""
    if os.environ.get("TAWG_MODAL_MAINTENANCE_ENABLED") != "true":
        return
    await repository_worker.spawn.aio(None)


@app.function(
    image=image,
    secrets=webhook_secrets,
    timeout=_ENDPOINT_TIMEOUT_SECONDS,
)
@modal.fastapi_endpoint(method="POST")
async def telegram_webhook(request: Request) -> Response:
    """Authenticate, normalize, and durably accept one Telegram update."""
    expected_secret = os.environ.get("TAWG_TELEGRAM_WEBHOOK_SECRET")
    if expected_secret is None or not is_valid_telegram_webhook_secret(expected_secret):
        return Response(status_code=503)
    supplied_secret = request.headers.get(_WEBHOOK_SECRET_HEADER)
    try:
        authenticated = supplied_secret is not None and hmac.compare_digest(
            supplied_secret.encode("ascii"),
            expected_secret.encode("ascii"),
        )
    except UnicodeEncodeError:
        authenticated = False
    if not authenticated:
        return Response(status_code=403)

    try:
        normalizer = _normalizer()
    except (KeyError, OSError, ValueError):
        return Response(status_code=503)

    try:
        body = await _bounded_body(request.stream())
    except Exception:
        return Response(status_code=400)
    if body is None:
        return Response(status_code=400)
    decision = normalizer.process(supplied_secret, body)
    if decision.disposition is TelegramWebhookDisposition.IGNORE:
        return Response(status_code=200)
    if decision.disposition is TelegramWebhookDisposition.REJECT or decision.envelope is None:
        return Response(status_code=400)
    try:
        await repository_worker.spawn.aio(decision.envelope.model_dump(mode="json"))
    except Exception:
        return Response(status_code=503)
    return Response(status_code=200)
