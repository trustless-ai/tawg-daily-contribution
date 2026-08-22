# Telegram history import

Export the configured group as machine-readable JSON from Telegram Desktop. Do not select media downloads.

Keep the export outside this repository. Preview it first:

```bash
python3.12 -m tawg_bot.cli import-telegram-history --input "$TAWG_EXPORT_PATH" --group-slug tawg --dry-run
```

Run again without `--dry-run` only after reviewing the counts. The command writes sanitized monthly JSONL and TAWG-local aliases; it never copies the export or media files.
