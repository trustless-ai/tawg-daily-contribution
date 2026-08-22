# GitHub Actions operator setup

The workflow is intentionally disabled until the operator supplies one Telegram group and the model backend settings. Test it first with `workflow_dispatch` and `observe_only=true` on the feature branch.

## Required GitHub configuration

Add these Actions secrets:

- `TELEGRAM_BOT_TOKEN`
- `ANTHROPIC_AUTH_TOKEN`

Add these Actions variables (the chat ID may instead be a secret):

- `TAWG_TELEGRAM_CHAT_ID` — the one allowed group ID
- `TAWG_TELEGRAM_BOT_USERNAME` — without the leading `@`
- `TAWG_DELIVERY_ENABLED` — keep unset or `false` until the live rollout gate
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `CLAUDE_CODE_SUBAGENT_MODEL`
- `CLAUDE_CODE_EFFORT_LEVEL`
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`

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

The workflow passes only this allowlisted model configuration to the tool-less, one-turn Claude Code process. Claude Code is pinned to `2.1.240`; auto-update, nonessential traffic, prompt history, sessions, tools, and MCP are disabled by the controller.

## Telegram prerequisites

The Bot must be an administrator in the one configured group. In BotFather, disable privacy mode so it receives ordinary group messages as well as mentions. Do not configure a webhook, and do not run any second `getUpdates` consumer: Telegram updates are consumed and cursor-committed by this single non-overlapping Actions writer.

Before live delivery, manually dispatch the feature branch with `observe_only=true`. Review the committed sanitized records, current knowledge pages, citations, and the prepared Daily artifact. Live Actions validation and secret entry are deliberately left to the operator.
