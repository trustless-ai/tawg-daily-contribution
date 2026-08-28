# Knowledge bot runbook

All times and windows below use UTC.

## Cursor stall

Inspect the relevant source cursor and latest stable record ID. Re-run the same workflow mode; ingestion is stable-ID upserted. Never advance a cursor by hand unless the corresponding source bytes are already committed and reviewed.

## Required-source failure

Scheduled discovery is intentionally limited to all public `trustless-ai` repositories (including archived repositories), each registered ERC's exact Magicians topic, and its optional exact `ethereum/ERCs` proposal PR. A failure records safe source labels and retains the last verified metadata for the failed source; it never falls back to scanning the full knowledge vault or the rest of `ethereum/ERCs`. Fix the credential/network/source issue and rerun. Explicit freshness-sensitive ERC answers still use their bounded live evidence path; a missing normative source must remain an explicit `not verified` gap and discussion never substitutes for it.

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

After deploying the open-knowledge rollout, run the versioned migration once in a clean checkout:

```bash
python3.12 -m tawg_bot.cli migrate-open-knowledge
```

It preserves Telegram records and knowledge bodies, archives the old refresh queue under
`data/state/migrations/open-knowledge-v1.json`, and backfills provenance only where existing
repository evidence is exact. A second run must be a no-op. A hash conflict is an incident to
diagnose, not permission to delete the migration audit or rerun against changed inputs.

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

The recurring scanner may write only `data/state/scoped-source-observations.json` and the GitHub/
Magicians portions of `data/state/source-cursors.json`. The observations contain stable locators,
timestamps, and metadata hashes—not post, issue, PR, comment, review, or repository-file bodies.

## Interactive knowledge writes

An explicit @bot request may record knowledge about any subject. Topic admission has no TAWG, ERC,
noun, or verb whitelist, but the transaction remains path-, evidence-, privacy-, and SHA-bounded.

- If the current audited conversation explicitly establishes that the requester or group authored
  the concept, the bot may preserve the supplied description in full.
- Otherwise it is external knowledge: require the original public HTTPS URL and store a neutral
  description of at most 2,000 characters plus that link.
- If authorship or the external original link is missing, the bot asks for the missing evidence and
  closes the current attempt as a normal READY clarification; it must not enter an automatic retry
  loop.

A complete recurring ERC registration must include `ERC-<number>` and the exact corresponding
Magicians topic URL; an exact `https://github.com/ethereum/ERCs/pull/<number>` proposal PR is
optional. The controller resolves and verifies the topic and optional PR metadata before updating
`knowledge/meta/scan-targets.yml`. A mismatch drops only the scan registration, so an independently
valid knowledge write can still succeed. Repeating the same target is idempotent; conflicting links
require an explicit reviewed correction.

## Source candidate promotion

A URL suggested in Telegram is normalized into `data/state/source-candidates.json` but is not fetched or trusted during that reply. To promote it:

1. Verify that it is public bounded text on an allowed HTTPS host/path and has no unsafe redirects.
2. Verify its relationship to the named ERC or TAWG topic.
3. Assign its evidence kind (`normative_spec`, `implementation`, `test`, `example`, or `discussion`) and authority (`canonical`, `official_org`, `maintainer`, or `community`).
4. Add a reviewed entry and bounded fetch policy to `knowledge/meta/sources.yml`; do not add source text. This makes the URL available to explicit evidence lookup only—it does not add a recurring scan target.
5. Run `check-sources --observe-only`, review the result, then let a later operation use it. Never fetch a newly suggested arbitrary URL in the same conversation turn.

Only a complete controller-verified ERC registration updates `knowledge/meta/scan-targets.yml`.
Never copy an ordinary candidate URL into that recurring registry.
