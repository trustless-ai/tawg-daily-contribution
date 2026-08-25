# Staged rollout

Promotion is explicit and sequential. A later gate never waives an earlier one.

1. **Bootstrap:** preview and import the Telegram Desktop JSON without media. Confirm only sanitized Telegram records are written and the raw export remains outside the repository.
2. **Source and knowledge acceptance:** audit `knowledge/meta/sources.yml`, then fetch GitHub, canonical ERC/EIP, and Ethereum Magicians evidence transiently. Review aliases, metadata-only source/claim ledgers, ERC pages, links, citations, gaps, and privacy lint. Confirm `data/github/` and `data/magicians/` do not exist.
3. **Observe-only L1:** manually dispatch `main` with `observe_only=true`; verify all ordinary group messages persist and only mentions create reply jobs.
4. **Daily dry run:** at or after a `23:00 UTC` cutoff, print and review at least one Daily for exactly `[previous 23:00 UTC, current 23:00 UTC)`. The command collects live external activity immediately before generation and neither writes an artifact nor delivers Telegram messages.
5. **Live Daily:** set `TAWG_DELIVERY_ENABLED=true` only after the target chat and exact UTC window are confirmed.
6. **Read-only mentions:** enable replies while correction transactions remain operationally unapproved.
7. **Corrections:** permit cited current-knowledge and TAWG-local alias transactions after reviewing the refusal and ambiguity behavior.

Rollback is non-destructive: unset `TAWG_DELIVERY_ENABLED`, disable the workflow, or return to observe-only. Do not delete sanitized Telegram history, metadata, cursors, delivery attempts, or Git history. An `ambiguous` delivery remains operator-reviewed; rollback is not permission to resend it.

Local knowledge compilation may use the developer's local Codex identity. The Actions runner uses the operator-configured DeepSeek endpoint through the pinned Claude Code CLI; Claude sessions are not durable, so repository state is the only long-term context. Validate the workflow manually on `main` with `observe_only=true` before enabling delivery.
