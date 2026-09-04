# Modal webhook operations

This guide covers the implemented Modal adapter in `deploy/modal_app.py`. Modal is only the
invocation wrapper: normalization, ingestion, scheduling, reply processing, delivery, and
repository checkpoints remain in the platform-neutral `tawg_bot` package and are also callable
from the CLI and GitHub Actions.

The same separation applies to source discovery. Modal's five-minute wrapper invokes the shared
maintenance pipeline; the wrapper contains no source-selection logic. The shared controller scans
only every public `trustless-ai` repository (including archived repositories), the exact Magicians
topics in `knowledge/meta/scan-targets.yml`, and each target's optional exact `ethereum/ERCs` PR.
GitHub Actions fallback runs that identical core. Do not add Modal-only repository, forum, or
knowledge-vault scanning.

## Safety invariants

- GitHub `main` is the canonical knowledge, state, and audit store. Modal storage is not a source
  of truth.
- Telegram permits either an outgoing webhook or `getUpdates`, not both. Keep exactly one
  Telegram consumer during every transition.
- Never use `drop_pending_updates=true`. Pending updates are retained during both cutover and
  rollback.
- Deploying `deploy/modal_app.py` never calls Telegram `setWebhook`. Both `setWebhook` and
  `deleteWebhook` are separately authorized manual operator actions.
- Never paste credentials or real Telegram identifiers into chat, issues, commits, fixture files,
  command arguments copied into tickets, or logs. Read them from an operator-controlled secret
  manager or hidden shell input and unset temporary variables afterward.
- Do not run a paid model for a deployment test. A real production reply may use the configured
  model only after the webhook path has passed deterministic and shadow checks.
- Do not automatically resend an `ambiguous` Telegram delivery.

The endpoint receives only the `tawg-webhook` Modal Secret. It authenticates the
`X-Telegram-Bot-Api-Secret-Token` header, validates the configured group, strips numeric chat and
user identities, and spawns one sanitized envelope. It cannot clone GitHub, call a model, or send
Telegram messages. The single-container `repository_worker` receives `tawg-worker`, checks out a
fresh `main`, and invokes the shared core. A separate five-minute scheduled adapter receives only
`tawg-maintenance`; it is a hard no-op unless `TAWG_MODAL_MAINTENANCE_ENABLED` is exactly `true`,
and then it spawns that same worker without calling `getUpdates`.

## Accounts, authentication, and secrets

Create or join the intended Modal workspace first. Authenticate locally from a private terminal;
the setup flow stores the Modal profile outside this repository:

```bash
uv run --with modal==1.5.4 modal setup
```

Run setup once and diagnose a failure before retrying. Do not paste the returned authentication
material into chat. CI uses the separately provisioned `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET` GitHub secrets described in
[`github-actions.md`](github-actions.md); they do not belong in the four runtime secrets below.

Create the runtime secrets in the Modal dashboard's Secrets panel. This is preferred because no
value appears in shell history or a process argument. The names and exact keys are:

- `tawg-webhook`
  - `TAWG_TELEGRAM_WEBHOOK_SECRET`
  - `TAWG_TELEGRAM_CHAT_ID`
  - `TAWG_TELEGRAM_BOT_USERNAME`
- `tawg-worker`
  - `TELEGRAM_BOT_TOKEN`
  - `TAWG_TELEGRAM_CHAT_ID`
  - `TAWG_TELEGRAM_BOT_USERNAME`
  - `ANTHROPIC_AUTH_TOKEN`
  - `ANTHROPIC_BASE_URL`
  - `ANTHROPIC_MODEL`
  - `ANTHROPIC_DEFAULT_OPUS_MODEL`
  - `ANTHROPIC_DEFAULT_SONNET_MODEL`
  - `ANTHROPIC_DEFAULT_HAIKU_MODEL`
  - `CLAUDE_CODE_SUBAGENT_MODEL`
  - `CLAUDE_CODE_EFFORT_LEVEL`
  - `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  - `GITHUB_TOKEN`
- `tawg-github-announcements`
  - `TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID`
- `tawg-maintenance`
  - `TAWG_MODAL_MAINTENANCE_ENABLED`

The GitHub token must be restricted to this repository and the contents-write permission required
by the reviewed checkpoint script. The webhook secret must be 32-256 ASCII characters from
`A-Z`, `a-z`, `0-9`, `_`, and `-`. The same value is supplied to Modal and later to Telegram's
`secret_token` parameter.

Set `TAWG_TELEGRAM_GITHUB_ANNOUNCEMENT_TOPIC_ID` to the positive Telegram
`message_thread_id` for the forum topic that receives GitHub PR and issue announcements. This
setting does not affect daily reports, ordinary replies, member welcomes, or knowledge updates.

Create `tawg-maintenance` with the exact value `false` for every shadow deployment. This secret is
the only credential available to the schedule trigger; it contains no repository, Telegram,
delivery, or model credential. Change it to the exact lowercase value `true` only during the
authorized cutover sequence below. Any missing, empty, differently cased, padded, or otherwise
invalid value remains disabled.

If dashboard entry is unavailable, `modal secret create` accepts `KEY="$EXISTING_VARIABLE"`
pairs from an operator-controlled shell. Use hidden `read -s` input, keep shell tracing disabled,
and unset every variable immediately afterward. Do not use a checked-in `.env`, JSON, or shell
file as an intermediate secret store.

## Verify and deploy without cutover

From a clean checkout of the reviewed commit:

```bash
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps .
python -m ruff check src tests deploy
python -m mypy src/tawg_bot
python -m pytest -q
python -m tawg_bot.cli vault-lint
git diff --check
```

The reviewed GitHub workflow is the preferred deploy path because it installs
`requirements-modal-deploy.lock` and runs every gate before deployment. For an explicitly
authorized local deployment, the supported shorthand is:

```bash
uv run --with modal==1.5.4 modal deploy deploy/modal_app.py
```

If the current environment does not already contain the project's pinned runtime dependencies,
install the hash-locked deployment environment instead of allowing dependency drift:

```bash
python -m pip install --require-hashes -r requirements-modal-deploy.lock
modal deploy deploy/modal_app.py
```

Record the reviewed commit and the `tawg-production` deployment URL in the private operator log.
Do not register it with Telegram yet. During shadow, keep `TAWG_RUNTIME_MODE=poll`, the GitHub
polling schedule active, `getWebhookInfo.result.url` empty, and `tawg-maintenance` set to exact
`false`. A shadow deployment must not perform scheduled repository work.

## Shadow validation

### 1. Deterministic fixture gate

These tests use fake Modal/FastAPI objects, fake Telegram identifiers, fake repository services,
and no paid model or external service:

```bash
python -m pytest -q \
  tests/unit/test_telegram_webhook.py \
  tests/unit/test_modal_adapter.py \
  tests/integration/test_telegram_webhook_intake.py
```

They must prove bad-secret rejection before body parsing, configured-chat filtering, numeric
identity removal before spawn, bounded input, duplicate/out-of-order ingestion, and a sanitized
spawn payload.

### 2. Shadow deployment observation

Deploy the reviewed app with the real secret names but do not send a supported update to the
deployed endpoint and do not register it with Telegram. Confirm only safe deployment metadata:

1. The deployed commit is the reviewed commit and the app name is `tawg-production`.
2. The public function has only `tawg-webhook`; the repository worker has only `tawg-worker`; the
   schedule trigger has only `tawg-maintenance`.
3. The worker is limited to one container. The five-minute schedule is visible, its maintenance
   flag is exact `false`, and observing a scheduled invocation confirms that it does not spawn the
   worker or create a repository commit.
4. `getWebhookInfo.result.url` remains empty, `TAWG_RUNTIME_MODE=poll`, and GitHub polling remains
   the only Telegram consumer.
5. Modal status contains no raw request body, numeric Telegram identity, credential, or unfiltered
   exception.

The no-paid synthetic dispatch proof stops at the local fake boundary in step 1. The implemented
production ingestion entrypoint does not have a test-only or ingest-only mode: after persisting any
supported envelope it also prepares and delivers all currently actionable reply jobs. A preflight
check for zero actionable jobs cannot close the race with a newly arriving real mention. Therefore
do not claim that a deployed synthetic request is model-free, and do not send one as a shadow test.
The deployed endpoint is first exercised in the controlled cutover window below, with normal
production model/delivery behavior expected and audited. If a deployed, no-paid synthetic dispatch
remains an acceptance requirement, add and review an explicit implementation boundary in a later
task rather than approximating it operationally.

## Webhook cutover

Cutover is a separately authorized production change. Schedule it immediately after a GitHub poll
finishes and confirm there is no in-flight polling run. The transition order preserves the no-drop
and single-consumer invariants:

1. Confirm the restored production `tawg-webhook`, `tawg-worker`, and disabled
   `tawg-maintenance` configurations, deployed commit, endpoint health, and Modal maintenance
   schedule.
2. Confirm `getWebhookInfo.result.url` is empty and GitHub's authoritative
   `TAWG_RUNTIME_MODE` is still `poll`.
3. Call `setWebhook` with the deployed HTTPS endpoint, the exact shared `secret_token`,
   `allowed_updates=["message", "edited_message"]`, `max_connections=1`, and
   `drop_pending_updates=false`.
4. Call `getWebhookInfo` immediately. Require the exact endpoint URL, the expected allowed
   updates, and no current synchronization error. Telegram makes `getUpdates` unavailable while
   this URL is set.
5. Immediately set the GitHub repository variable `TAWG_RUNTIME_MODE=webhook`, then disable the
   scheduled `TAWG knowledge bot` Actions workflow through the authorized GitHub operation. Keep
   the workflow file and manual fallback intact for rollback, but confirm no Actions run remains
   in flight. Mode alone is insufficient here: `maintenance-tick` still processes replies and
   would race the Modal worker.
6. Only after the Actions schedule is disabled and idle, change
   `TAWG_MODAL_MAINTENANCE_ENABLED` in `tawg-maintenance` from exact `false` to exact lowercase
   `true`. Redeploy the exact reviewed commit that is currently on `main`, verify the recorded SHA,
   and confirm the schedule trigger still has no worker credential. Do not enable maintenance by
   editing source or adding worker credentials to the trigger.
7. Only after that exact-`main` redeploy succeeds, send one real ordinary group message and
   one real @bot mention in its intended topic/thread. This is the first deployed endpoint
   exercise; normal production model and Telegram delivery are expected for the mention.
8. Verify the ordinary message and mention each produce exactly one sanitized repository record;
   the mention produces one stable reply job and one delivery attempt.
9. Verify the delivery is `delivered`, `reply_to_message_id` equals the trigger message, and
   `message_thread_id` equals the trigger thread ID (or is `null` only for a non-topic message).
   Confirm the visible Telegram reply is attached to the same message/thread.
10. Keep the scheduled Actions workflow disabled. Modal is the only production scheduler and reply
   worker as well as the only Telegram consumer. The retained workflow definition remains the
   operator-triggered fallback after the rollback sequence below.

Never combine steps 3 and 5 into deployment automation. If the acceptance check fails, roll back;
do not temporarily re-enable `getUpdates` while the webhook URL is active.

### Safe Bot API helper

Telegram places the bot token in the request URL. Avoid `curl -v`, shell tracing, command-line URL
arguments, and unfiltered exceptions. The following helper reads secrets from the environment and
prints only allowlisted result fields. Set `TAWG_BOT_API_METHOD` to `setWebhook`,
`getWebhookInfo`, or `deleteWebhook`; set `TAWG_MODAL_WEBHOOK_URL` only for `setWebhook`.

```bash
python - <<'PY'
import json
import os
import urllib.error
import urllib.request

method = os.environ["TAWG_BOT_API_METHOD"]
if method == "setWebhook":
    payload = {
        "url": os.environ["TAWG_MODAL_WEBHOOK_URL"],
        "secret_token": os.environ["TAWG_TELEGRAM_WEBHOOK_SECRET"],
        "allowed_updates": ["message", "edited_message"],
        "max_connections": 1,
        "drop_pending_updates": False,
    }
elif method == "deleteWebhook":
    payload = {"drop_pending_updates": False}
elif method == "getWebhookInfo":
    payload = {}
else:
    raise SystemExit("unsupported Bot API method")

url = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/" + method
request = urllib.request.Request(
    url,
    data=json.dumps(payload, separators=(",", ":")).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read())
except (OSError, ValueError, urllib.error.HTTPError):
    raise SystemExit("Telegram Bot API request failed; inspect status privately") from None
if result.get("ok") is not True:
    raise SystemExit("Telegram Bot API rejected the request")
if method == "getWebhookInfo":
    info = result.get("result", {})
    print(json.dumps({
        "url": info.get("url", ""),
        "pending_update_count": info.get("pending_update_count"),
        "allowed_updates": info.get("allowed_updates", []),
        "last_error_date": info.get("last_error_date"),
        "last_synchronization_error_date": info.get("last_synchronization_error_date"),
    }, sort_keys=True))
else:
    print("ok=true")
PY
```

Unset `TELEGRAM_BOT_TOKEN`, `TAWG_TELEGRAM_WEBHOOK_SECRET`, and temporary endpoint/method
variables when the authorized operation is complete.

## Rollback without dropping updates

Rollback is an operator action, not a redeploy side effect:

1. Stop accepting new operational changes and inspect any `ambiguous` delivery; do not resend it.
2. Change `TAWG_MODAL_MAINTENANCE_ENABLED` in `tawg-maintenance` to exact `false`, redeploy the
   exact reviewed commit currently on `main`, and verify its SHA. Wait for every already-spawned
   `repository_worker` invocation that can finish promptly before deleting the webhook; changing
   the flag does not cancel work already in flight. This pre-delete drain reduces overlap risk but
   does not replace the mandatory post-delete drain in step 5.
3. Call `deleteWebhook` with `drop_pending_updates=false` using the safe helper.
4. Call `getWebhookInfo` and require `getWebhookInfo.result.url` to be empty. Do not enable polling
   until this is true. The reported pending count may be non-zero and must not be discarded.
5. After the URL is empty, confirm a subsequent Modal schedule invocation is a no-op. Require that
   all retries are exhausted and zero active, queued, or retrying `repository_worker` calls remain,
   so GitHub fallback can become the sole scheduler.
6. Set `TAWG_RUNTIME_MODE=poll`, restore/enable the GitHub schedule, and run at most one explicit
   polling fallback. Never run a second local `tick` concurrently.
7. Confirm retained Telegram updates are ingested, then reconcile `pending` and `ready` jobs,
   `ambiguous` deliveries, `data/state/telegram-webhook-receipts.json`, and the polling cursor from
   canonical repository state.
8. Run vault lint and review the resulting commits. Keep all sanitized history, receipts, cursors,
   delivery attempts, and Git history.

Telegram retains incoming updates for no longer than 24 hours, so execute an unhealthy-runtime
rollback promptly. Diagnose and re-run the full shadow/cutover gates before another webhook
attempt.

## Dev bot mirror

The `dev` branch hosts a second, developer-facing bot ("dev bot") that shares the production
Python core but must never write canonical message history, knowledge, pending jobs, delivery
state, or layer markers.

Deployment contract:

- The dev Modal app is `tawg-development`, deployed from `.github/workflows/modal-deploy-dev.yml`
  with `TAWG_MODAL_BRANCH=dev`, `TAWG_REPOSITORY_PERSIST_MODE=receipt-only`, and
  `TAWG_DEV_MODE=true`. Production (`modal-deploy.yml`) sets `TAWG_DEV_MODE=false`.
- Dev and production share the same Modal workspace and the same shared secrets (`tawg-worker`,
  `tawg-webhook`, `tawg-maintenance`, `tawg-github-announcements`). The three bot-specific values
  differ and live in one extra secret `tawg-dev` under their production-matching names:
  - `TELEGRAM_BOT_TOKEN` (dev bot token)
  - `TAWG_TELEGRAM_BOT_USERNAME` (dev bot username, without `@`)
  - `TAWG_TELEGRAM_WEBHOOK_SECRET` (dev webhook secret)
  In dev mode `tawg-dev` is mounted after the shared secret on `repository_worker` and
  `telegram_webhook`; Modal applies secrets in order, so the dev values override the shared ones.
  Production never mounts `tawg-dev`.
- The dev bot's numeric identity is derived from its token prefix. In `receipt-only` mode the dev
  bot deduplicates on its own namespaced receipt `data/state/telegram-webhook-receipts.<bot_id>.json`
  and never falls back to the production legacy receipt.
- Production runs `full` mode, which is the only mode that falls back to the legacy unnamed
  receipt for the one-time migration to the namespaced path.
- Each dev run merges `origin/main` into `dev` first and fails closed on conflict (no receipt
  write, no Telegram send). The shared five-minute `scheduled_maintenance` schedule keeps `dev`
  advancing when the dev bot is idle (gated by `TAWG_MODAL_MAINTENANCE_ENABLED`); on the dev branch
  the worker only merges and never runs the Daily digest, L1–L4, a model, or a send.
