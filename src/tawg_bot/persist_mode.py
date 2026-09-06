from __future__ import annotations

import os
from enum import StrEnum


class PersistMode(StrEnum):
    FULL = "full"
    RECEIPT_ONLY = "receipt-only"
    NONE = "none"


def configured_persist_mode() -> PersistMode:
    raw = os.environ.get("TAWG_REPOSITORY_PERSIST_MODE")
    if raw:
        try:
            return PersistMode(raw)
        except ValueError:
            raise RuntimeError(
                "TAWG_REPOSITORY_PERSIST_MODE must be full, receipt-only, or none"
            ) from None
    enabled = os.environ.get("TAWG_REPOSITORY_PERSIST_ENABLED", "true")
    return PersistMode.NONE if enabled == "false" else PersistMode.FULL
