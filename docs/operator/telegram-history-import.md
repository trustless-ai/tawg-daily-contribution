# Telegram history import

Export the configured group as machine-readable JSON from Telegram Desktop. Do not select media downloads.

Keep the export outside this repository. Preview it first:

```bash
python3.12 -m tawg_bot.cli import-telegram-history --input "$TAWG_EXPORT_PATH" --group-slug tawg --dry-run
```

The importer rejects exports whose immutable Telegram chat ID or display name does not
match `telegram.expected_export_id` and `telegram.expected_export_name` in
`config/sources.yml`. Keep those values aligned with the one rollout group.

Run again without `--dry-run` only after reviewing the counts. The command writes sanitized
monthly JSONL and TAWG-local aliases; it never persists the raw export or media files. Parsing
uses an access-restricted anonymous temporary snapshot that is deleted automatically when the
command exits.
