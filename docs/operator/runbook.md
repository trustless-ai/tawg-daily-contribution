# Knowledge bot runbook

All times and windows below use UTC.

## Cursor stall

Inspect the relevant source cursor and latest stable record ID. Re-run the same workflow mode; ingestion is stable-ID upserted. Never advance a cursor by hand unless the corresponding source bytes are already committed and reviewed.

## Required-source failure

GitHub repository failures, required Magicians topic failures, canonical ERC/EIP failures, or Telegram intake failures block the affected heavier work and Daily delivery. Fix the credential/network/source issue and rerun. A missing normative source must remain an explicit `not verified` gap; never substitute a discussion post for it. The old layer-success time keeps the same work due.

## Rejected model output

Inspect only safe controller errors and the cited evidence. Path, privacy, schema, citation, ledger, or vault-lint rejection leaves canonical knowledge and its semantic cursor unchanged. Do not bypass the validator; correct the source or prompt policy and retry.

## Ambiguous Telegram delivery

If state is `ambiguous`, inspect the configured group manually for the stored window/job and message metadata. Do not automatically resend. Resolve the state only through an operator-reviewed repository change.

## Alias conflict

Keep identity scope inside this TAWG. Ask for an explicit merge and supporting public handle evidence. Never infer a cross-community identity or reuse the TAWG person ID elsewhere.

## Privacy rejection

Remove or redact contact details, numeric Telegram identities, local paths, credentials, non-allowlisted wallets, or private-chat material at ingestion. Do not add a privacy exception merely to make a run pass.

## Manual commands

Run these from the repository root with the package installed or `PYTHONPATH=src`. The first four are local no-write checks: they do not commit, push, or deliver Telegram messages.

```bash
python3.12 -m tawg_bot.cli vault-lint
python3.12 -m tawg_bot.cli check-sources --erc 8004 --observe-only
python3.12 -m tawg_bot.cli refresh-knowledge --erc 8004 --dry-run
python3.12 -m tawg_bot.cli daily-dry-run --window-end 2026-08-23T23:00:00Z
```

`daily-dry-run` prints the prepared English message to stdout and keeps it out of repository state. Its window is exactly `[2026-08-22T23:00:00Z, 2026-08-23T23:00:00Z)` in this example, and the end must not be in the future. GitHub and Magicians content is collected live after the cutoff is known, used in memory, and discarded.

`tick --observe-only` is not a no-write local preview: normal runtime checkpoints may commit and publish safe repository state. Reserve it for the staged Actions rollout or replace its checkpoint in a controlled test harness.

Run real Actions and enter secrets only through the GitHub or Modal UI. Keep Telegram privacy mode disabled. In polling mode, keep the webhook unset and exactly one `getUpdates` consumer. In webhook mode, keep the verified webhook set, run no `getUpdates` consumer, and use only `maintenance-tick` for scheduled maintenance. See [`modal.md`](modal.md) for the authorized transition order.

During Modal shadow, `TAWG_MODAL_MAINTENANCE_ENABLED` is exactly `false`; GitHub polling remains
the only scheduler and Telegram consumer.

## Runtime-mode incident boundary

`TAWG_RUNTIME_MODE` is authoritative. `poll` invokes `tick`; `webhook` invokes
`maintenance-tick`; `observe` uses the corresponding command without delivery. Never switch to
polling while `getWebhookInfo.result.url` is non-empty. A deployment does not authorize
`setWebhook`, `deleteWebhook`, or a runtime-mode change.

After production webhook registration, disable the scheduled Actions workflow and confirm no run
is active. Only after Actions is disabled and idle may you set
`TAWG_MODAL_MAINTENANCE_ENABLED` to exact lowercase `true` and redeploy the exact reviewed commit
currently on `main`; only then send the acceptance mention. `maintenance-tick` skips `getUpdates`
but still processes replies, so running it beside the Modal worker would create two reply workers.
Retain the workflow definition for manual fallback; re-enable it only after the no-drop rollback
sequence. The full authoritative order is [`modal.md`](modal.md#webhook-cutover).

For webhook failure, preserve queued updates. First set `TAWG_MODAL_MAINTENANCE_ENABLED` to exact
`false` and redeploy the exact reviewed `main`. A pre-delete drain is useful but cannot replace the
mandatory post-delete drain. Then call `deleteWebhook` with `drop_pending_updates=false` and verify
`getWebhookInfo.result.url` is empty. After the URL is empty, require that all retries are exhausted
and zero active, queued, or retrying `repository_worker` calls remain. Only afterward restore
`TAWG_RUNTIME_MODE=poll` and the single scheduled Actions polling consumer. Reconcile canonical
pending jobs and delivery state; do not delete receipts/cursors or automatically resend
`ambiguous` deliveries. The complete authoritative procedure is in
[`modal.md`](modal.md#rollback-without-dropping-updates).

## Persistence audit

After any non-dry run, verify that `data/telegram/**` contains only sanitized group records and that `knowledge/**` contains generated synthesis. External source state may contain URLs, source keys, versions, hashes, byte counts, UTC observation times, gaps, and jobs only. `data/github/**`, `data/magicians/**`, fetched bodies, credentials, URL query secrets, and raw Telegram exports must never appear.

```bash
test ! -d data/github
test ! -d data/magicians
python3.12 -m tawg_bot.cli vault-lint
git diff --check
git status --short
```

## Source candidate promotion

A URL suggested in Telegram is normalized into `data/state/source-candidates.json` but is not fetched or trusted during that reply. To promote it:

1. Verify that it is public bounded text on an allowed HTTPS host/path and has no unsafe redirects.
2. Verify its relationship to the named ERC or TAWG topic.
3. Assign its evidence kind (`normative_spec`, `implementation`, `test`, `example`, or `discussion`) and authority (`canonical`, `official_org`, `maintainer`, or `community`).
4. Add a reviewed entry and bounded fetch policy to `knowledge/meta/sources.yml`; do not add source text.
5. Run `check-sources --observe-only`, review the result, then let a later operation use it. Never fetch a newly suggested arbitrary URL in the same conversation turn.
