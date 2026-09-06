from tawg_bot.persist_mode import PersistMode, configured_persist_mode


def test_configured_persist_mode_explicit(monkeypatch):
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_MODE", "receipt-only")
    assert configured_persist_mode() is PersistMode.RECEIPT_ONLY


def test_configured_persist_mode_backcompat_disabled(monkeypatch):
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_MODE", raising=False)
    monkeypatch.setenv("TAWG_REPOSITORY_PERSIST_ENABLED", "false")
    assert configured_persist_mode() is PersistMode.NONE


def test_configured_persist_mode_default_full(monkeypatch):
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_MODE", raising=False)
    monkeypatch.delenv("TAWG_REPOSITORY_PERSIST_ENABLED", raising=False)
    assert configured_persist_mode() is PersistMode.FULL
