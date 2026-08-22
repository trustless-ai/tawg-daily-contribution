import json
from pathlib import Path

from tawg_bot.telegram_export import TelegramDesktopImporter
from tawg_bot.unit_of_work import RepositoryUnitOfWork

ROOT = Path(__file__).parents[2]


def test_history_import_publishes_monthly_records_and_aliases(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "knowledge/meta").mkdir(parents=True)
    (tmp_path / "config/privacy.yml").write_bytes((ROOT / "config/privacy.yml").read_bytes())
    (tmp_path / "knowledge/meta/aliases.yml").write_text(
        "schema: tawg.aliases.v1\nscope: tawg-only\npeople: {}\n"
    )
    importer = TelegramDesktopImporter.for_repository(tmp_path)
    uow = RepositoryUnitOfWork(tmp_path, operation_id="history-import")

    report = importer.import_file(
        ROOT / "tests/fixtures/telegram_export.json", group_slug="tawg", uow=uow
    )
    changed = uow.publish().changed_paths

    assert report.imported == 5
    assert "data/telegram/2026/08/messages.jsonl" in changed
    records = (tmp_path / "data/telegram/2026/08/messages.jsonl").read_text().splitlines()
    assert len(records) == 5
    assert all(json.loads(line)["record_id"].startswith("tg:tawg:") for line in records)
    assert "knowledge/meta/aliases.yml" in changed

    replay = TelegramDesktopImporter.for_repository(tmp_path)
    replay_uow = RepositoryUnitOfWork(tmp_path, operation_id="history-replay")
    replay.import_file(
        ROOT / "tests/fixtures/telegram_export.json", group_slug="tawg", uow=replay_uow
    )

    assert replay_uow.publish().changed_paths == ()
