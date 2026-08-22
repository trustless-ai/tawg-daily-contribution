# Staged rollout

Promotion is explicit and sequential. A later gate never waives an earlier one.

1. **Bootstrap:** import the sanitized Telegram Desktop JSON and backfill GitHub and Ethereum Magicians. Confirm no raw export or media is tracked.
2. **Knowledge acceptance:** review aliases, source/claim ledgers, ERC pages, links, citations, and privacy lint. Resolve ambiguity before promotion.
3. **Observe-only L1:** manually dispatch the feature branch with `observe_only=true`; verify all ordinary group messages persist and only mentions create reply jobs.
4. **Daily dry run:** produce and review at least one fixed-window Daily artifact without Telegram delivery.
5. **Live Daily:** set `TAWG_DELIVERY_ENABLED=true` only after the target chat and exact UTC window are confirmed.
6. **Read-only mentions:** enable replies while correction transactions remain operationally unapproved.
7. **Corrections:** permit cited current-knowledge and TAWG-local alias transactions after reviewing the refusal and ambiguity behavior.

Rollback is non-destructive: unset `TAWG_DELIVERY_ENABLED`, disable the workflow, or return to observe-only. Do not delete source history, cursors, delivery attempts, or Git history. An `ambiguous` delivery remains operator-reviewed; rollback is not permission to resend it.

