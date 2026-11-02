"""Runtime feature switches shared by intake, routing, and deployment."""

from __future__ import annotations

import os

ENTRY_CANDIDATES_ENV = "TAWG_ENTRY_CANDIDATES_ENABLED"
ENTRY_ROUTER_VERSION = "contextual-ai-v6"


def entry_candidates_enabled() -> bool:
    """Return whether the dev-only entry-decision path is enabled for this process."""
    return os.environ.get(ENTRY_CANDIDATES_ENV, "false").casefold() == "true"
