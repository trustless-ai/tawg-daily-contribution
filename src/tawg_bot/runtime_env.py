from __future__ import annotations

import os

_DEV_MODE_ENV = "TAWG_DEV_MODE"


def is_dev_mode() -> bool:
    return os.environ.get(_DEV_MODE_ENV) == "true"


def resolve_env(name: str) -> str | None:
    if is_dev_mode():
        dev_value = os.environ.get(f"dev_{name}")
        if dev_value:
            return dev_value
    return os.environ.get(name)


def require_env(name: str) -> str:
    value = resolve_env(name)
    if value is None:
        raise KeyError(name)
    return value
