# Knowledge bot runbook

All times and windows below use UTC.

## Cursor stall

Inspect the relevant source cursor and latest stable record ID. Re-run the same workflow mode; ingestion is stable-ID upserted. Never advance a cursor by hand unless the corresponding source bytes are already committed and reviewed.

## Required-source failure

GitHub repository failures, required Magicians topic failures, or Telegram intake failures block heavier work and Daily delivery. Fix the credential/network/source issue and rerun. The old layer-success time keeps the same work due.

## Rejected model output

Inspect only safe controller errors and the cited evidence. Path, privacy, schema, citation, ledger, or vault-lint rejection leaves canonical knowledge and its semantic cursor unchanged. Do not bypass the validator; correct the source or prompt policy and retry.

## Ambiguous Telegram delivery

If state is `ambiguous`, inspect the configured group manually for the stored window/job and message metadata. Do not automatically resend. Resolve the state only through an operator-reviewed repository change.

## Alias conflict

Keep identity scope inside this TAWG. Ask for an explicit merge and supporting public handle evidence. Never infer a cross-community identity or reuse the TAWG person ID elsewhere.

## Privacy rejection

Remove or redact contact details, numeric Telegram identities, local paths, credentials, non-allowlisted wallets, or private-chat material at ingestion. Do not add a privacy exception merely to make a run pass.

## Manual commands

```bash
python -m tawg_bot.cli tick --now 2026-08-23T23:00:00Z --observe-only
python -m tawg_bot.cli backfill github
python -m tawg_bot.cli backfill magicians
python -m tawg_bot.cli daily-dry-run --window-end 2026-08-23T23:00:00Z
python -m tawg_bot.cli vault-lint
```

Run real Actions and enter secrets only through the GitHub UI. Keep Telegram privacy mode disabled, webhook unset, and exactly one `getUpdates` consumer.

