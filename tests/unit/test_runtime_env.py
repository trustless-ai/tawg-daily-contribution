import pytest

from tawg_bot.runtime_env import is_dev_mode, require_env, resolve_env


def test_not_dev_mode_reads_plain_var(monkeypatch):
    monkeypatch.delenv("TAWG_DEV_MODE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "prod-token")
    monkeypatch.setenv("dev_TELEGRAM_BOT_TOKEN", "dev-token")

    assert is_dev_mode() is False
    assert resolve_env("TELEGRAM_BOT_TOKEN") == "prod-token"


def test_dev_mode_prefers_dev_prefix(monkeypatch):
    monkeypatch.setenv("TAWG_DEV_MODE", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "prod-token")
    monkeypatch.setenv("dev_TELEGRAM_BOT_TOKEN", "dev-token")

    assert is_dev_mode() is True
    assert resolve_env("TELEGRAM_BOT_TOKEN") == "dev-token"


def test_dev_mode_falls_back_when_no_dev_prefix(monkeypatch):
    monkeypatch.setenv("TAWG_DEV_MODE", "true")
    monkeypatch.setenv("TAWG_TELEGRAM_CHAT_ID", "-100123")

    assert resolve_env("TAWG_TELEGRAM_CHAT_ID") == "-100123"


def test_dev_mode_only_exact_true_is_dev(monkeypatch):
    for value in ("false", "", "True", "1", " TRUE ", "true "):
        monkeypatch.setenv("TAWG_DEV_MODE", value)
        assert is_dev_mode() is False, f"unexpected dev mode for {value!r}"


def test_require_env_raises_key_error_when_missing(monkeypatch):
    monkeypatch.delenv("TAWG_DEV_MODE", raising=False)
    monkeypatch.delenv("TAWG_TELEGRAM_BOT_USERNAME", raising=False)
    with pytest.raises(KeyError):
        require_env("TAWG_TELEGRAM_BOT_USERNAME")


def test_require_env_reads_dev_prefix_in_dev_mode(monkeypatch):
    monkeypatch.setenv("TAWG_DEV_MODE", "true")
    monkeypatch.setenv("TAWG_TELEGRAM_BOT_USERNAME", "prod_bot")
    monkeypatch.setenv("dev_TAWG_TELEGRAM_BOT_USERNAME", "dev_bot")
    assert require_env("TAWG_TELEGRAM_BOT_USERNAME") == "dev_bot"
