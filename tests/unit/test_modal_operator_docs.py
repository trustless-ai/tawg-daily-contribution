from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR_DOCS = ROOT / "docs" / "operator"


def _text(name: str) -> str:
    return (OPERATOR_DOCS / name).read_text(encoding="utf-8")


def test_manual_actions_observe_mode_uses_workflow_input() -> None:
    rollout = _text("rollout.md")
    manual_testing = _text("manual-testing.md")

    assert "manually dispatch `main` with input `runtime_mode=observe`" in rollout
    assert "Validate the workflow manually on `main` with input `runtime_mode=observe`" in rollout
    assert "`workflow_dispatch` input `runtime_mode=observe`" in manual_testing

    assert "manually dispatch `main` with `TAWG_RUNTIME_MODE=observe`" not in rollout
    assert "`workflow_dispatch` 且保持 `TAWG_RUNTIME_MODE=observe`" not in manual_testing


def test_modal_rollback_docs_require_post_delete_worker_drain_before_polling() -> None:
    anchors = {
        "modal.md": "## Rollback without dropping updates",
        "rollout.md": "Rollback is non-destructive.",
        "manual-testing.md": "失败时不要直接启动 polling。",
        "runbook.md": "For webhook failure, preserve queued updates.",
    }
    drain_markers = {
        "modal.md": (
            "all retries are exhausted and zero active, queued, or retrying "
            "`repository_worker` calls remain",
        ),
        "rollout.md": (
            "all retries are exhausted and zero active, queued, or retrying "
            "`repository_worker` calls remain",
        ),
        "manual-testing.md": (
            "所有重试均已耗尽",
            "active、queued、retrying 状态的 `repository_worker` call 全部为零",
        ),
        "runbook.md": (
            "all retries are exhausted and zero active, queued, or retrying "
            "`repository_worker` calls remain",
        ),
    }

    for name, anchor in anchors.items():
        rollback = " ".join(_text(name).split(anchor, maxsplit=1)[1].split())
        ordered_markers = (
            "`false`",
            "`deleteWebhook",
            "`getWebhookInfo.result.url`",
            *drain_markers[name],
            "`TAWG_RUNTIME_MODE=poll`",
        )
        positions = [rollback.index(marker) for marker in ordered_markers]
        assert positions == sorted(positions), name
