# GitHub Actions operator setup

The knowledge workflow runs only when `github.ref` is exactly `refs/heads/main`; manual dispatches from feature branches are refused before any job step can receive runtime credentials or write repository contents. Supply one Telegram group and the model backend settings, then test it with `workflow_dispatch` and `runtime_mode=observe` before enabling live delivery.

## Required GitHub configuration

Add these Actions secrets:

- `TELEGRAM_BOT_TOKEN`
- `ANTHROPIC_AUTH_TOKEN`
- `MODAL_TOKEN_ID` — used only by the manual Modal deployment workflow
- `MODAL_TOKEN_SECRET` — used only by the manual Modal deployment workflow

Add these Actions variables (the chat ID may instead be a secret):

- `TAWG_TELEGRAM_CHAT_ID` — the one allowed group ID
- `TAWG_TELEGRAM_BOT_USERNAME` — without the leading `@`
- `TAWG_DELIVERY_ENABLED` — keep unset or `false` until the live rollout gate
- `TAWG_RUNTIME_MODE` — authoritative runtime mode: `poll`, `webhook`, or `observe`; unset defaults to `poll`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `CLAUDE_CODE_SUBAGENT_MODEL`
- `CLAUDE_CODE_EFFORT_LEVEL`
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`

Only the final knowledge operation step receives `GITHUB_TOKEN` from `${{ github.token }}` and the Telegram/model runtime configuration; checkout, setup, installation, tests, and vault lint receive none of those runtime credentials. Checkout does not persist a credential. The operation step supplies Git authentication through transient process configuration, clears it on exit, and never writes the token to repository configuration, command arguments, URLs, or logs. Do not create a second token unless organization policy requires one. Give the workflow only the repository-content permission needed for its reviewed checkpoint writes and only public organization read access for collection. The Modal deployment workflow has read-only repository access and receives only `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` for deployment. The Claude Code child process receives only its explicit model/backend allowlist and never GitHub or Telegram credentials.

## Runtime-mode controls

`TAWG_RUNTIME_MODE` is the authoritative operational state. When it is unset, scheduled and inherited operations use `poll`, preserving the five-minute polling schedule during shadow deployment. `webhook` runs only repository maintenance through `maintenance-tick`; `observe` runs the appropriate polling or maintenance command with `--observe-only`.

Manual runs default to `runtime_mode=observe`. Operators may choose `inherit`, `observe`, `poll`, or `webhook`; a manual `poll` request fails before it can call `tick` if the authoritative mode is `webhook`. Regardless of mode, any value other than literal `true` for `TAWG_DELIVERY_ENABLED` forces the selected command into observe-only operation.

## Modal deployment configuration

Create these Modal secret objects outside the repository, with no secret values committed to GitHub:

- `tawg-webhook` — `TAWG_TELEGRAM_WEBHOOK_SECRET`, `TAWG_TELEGRAM_CHAT_ID`, and `TAWG_TELEGRAM_BOT_USERNAME`
- `tawg-worker` — `TELEGRAM_BOT_TOKEN`, the Telegram group variables, model configuration variables, and a repository-scoped `GITHUB_TOKEN` with only the required contents-write access

The manual `Deploy TAWG Modal app` Actions workflow installs the complete hash-locked deployment dependencies (including `modal==1.5.4` and `fastapi==0.141.1`), then verifies Ruff, mypy, the full test suite, and vault lint before deploying `deploy/modal_app.py`. Its GitHub deploy credentials are the `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` secrets listed above; do not place their values in workflow files, logs, issue comments, or chat. During shadow deployment, it does not configure Telegram's webhook. Webhook registration is an explicit manual cutover action and is never performed by a workflow.

Before using the deployment workflow, create a protected GitHub Environment named `tawg-production`. Restrict its deployment branches to `main` and require an operator approval through required reviewers. The workflow additionally refuses non-`main` refs, checks out the dispatched `${{ github.sha }}` exactly, and verifies the checked-out `HEAD` before any validation or deployment command runs.

DeepSeek-compatible example:

```yaml
ANTHROPIC_AUTH_TOKEN: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
ANTHROPIC_BASE_URL: https://api.deepseek.com/anthropic
ANTHROPIC_MODEL: deepseek-v4-pro[1m]
ANTHROPIC_DEFAULT_OPUS_MODEL: deepseek-v4-pro[1m]
ANTHROPIC_DEFAULT_SONNET_MODEL: deepseek-v4-pro[1m]
ANTHROPIC_DEFAULT_HAIKU_MODEL: deepseek-v4-flash
CLAUDE_CODE_SUBAGENT_MODEL: deepseek-v4-flash
CLAUDE_CODE_EFFORT_LEVEL: max
CLAUDE_CODE_AUTO_COMPACT_WINDOW: "786432"
```

The workflow passes only this allowlisted model configuration to a tool-less, single-invocation Claude Code process with `--max-turns 1`. Claude Code is pinned to `2.1.240`; auto-update, nonessential traffic, prompt history, sessions, tools, and MCP are disabled by the controller. A provider may report the internal JSON-schema handoff as `num_turns=2`; the harness accepts only a successful `stop_reason=tool_use` result that already contains structured output. Each invocation receives a bounded context pack and ends without a resumable session. Durable context consists of sanitized Telegram history, generated knowledge, and body-free source metadata in the repository.

Local compilation may instead use the developer's authenticated Codex setup. Those local credentials are not copied to Actions; Actions uses the DeepSeek-compatible variables above through Claude Code CLI.

## Telegram prerequisites

The Bot must be an administrator in the one configured group. In BotFather, disable privacy mode so it receives ordinary group messages as well as mentions. During shadow rollout, and whenever `TAWG_RUNTIME_MODE=poll`, do not configure a webhook or run any second `getUpdates` consumer: Telegram updates are consumed and cursor-committed by this single non-overlapping Actions writer. Webhook registration is an explicit manual cutover action, never a workflow action.

Before live delivery, manually dispatch `main` with `runtime_mode=observe`. Review the committed sanitized records, current knowledge pages, metadata, citations, and absence of external-body mirrors. The scheduled Daily cutoff is `23:00 UTC`, with live activity collected for `[previous 23:00 UTC, current 23:00 UTC)` immediately before generation. Live Actions validation and secret entry remain operator-controlled.
