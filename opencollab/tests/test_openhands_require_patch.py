from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SWEBENCH_DIR = Path(__file__).resolve().parents[2] / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import openhands_require_patch as guard  # noqa: E402


def _env(tmp_path: Path, *, rejections: int = 2) -> dict[str, str]:
    return {
        "OPENHANDS_CONTAINER_ID": "container-123",
        "OPENHANDS_WORKSPACE": "/testbed",
        "OPENHANDS_OUTPUT_DIR": str(tmp_path),
        "OPENHANDS_EMPTY_PATCH_REJECTIONS": str(rejections),
    }


def test_stop_guard_allows_non_test_source_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *args: "diff --git a/app/main.py b/app/main.py\n+changed\n",
    )

    result = guard.evaluate_stop(_env(tmp_path))

    assert result["decision"] == "allow"
    assert result["reason"] == "source_patch_present"


def test_stop_guard_rejects_empty_or_test_only_patch_then_allows_at_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *args: "diff --git a/tests/test_app.py b/tests/test_app.py\n+changed\n",
    )
    env = _env(tmp_path, rejections=2)

    first = guard.evaluate_stop(env)
    second = guard.evaluate_stop(env)
    third = guard.evaluate_stop(env)

    assert first["decision"] == "deny"
    assert second["decision"] == "deny"
    assert "Only validation/test files changed" in first["additionalContext"]
    assert third == {
        "decision": "allow",
        "reason": "empty_patch_rejection_limit_reached",
    }
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state["rejections"] == 2
    assert state["exhausted"] is True


def test_stop_guard_allows_hook_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args):
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(guard, "_container_patch", fail)

    result = guard.evaluate_stop(_env(tmp_path))

    assert result["decision"] == "allow"
    assert result["reason"] == "patch_guard_error"
