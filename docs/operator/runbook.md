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

Run real Actions and enter secrets only through the GitHub UI. Keep Telegram privacy mode disabled, webhook unset, and exactly one `getUpdates` consumer.

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
