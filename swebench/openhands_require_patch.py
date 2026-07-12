"""OpenHands stop hook that rejects completion without a source patch."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from gen_prediction_workflow import (  # noqa: E402
    _looks_like_validation_artifact,
    _patch_paths,
)


def _state_path(env: Mapping[str, str]) -> Path:
    output_dir = Path(env.get("OPENHANDS_OUTPUT_DIR") or ".")
    return output_dir / "empty_patch_stop_guard.json"


def _load_rejections(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(value.get("rejections") or 0) if isinstance(value, dict) else 0


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _container_patch(container_id: str, workspace: str) -> str:
    command = f"cd {shlex.quote(workspace)} && git diff --binary HEAD --"
    result = subprocess.run(
        ["docker", "exec", container_id, "sh", "-lc", command],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"container diff failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _source_paths(patch: str) -> tuple[list[str], list[str]]:
    paths = _patch_paths(patch)
    validation = [path for path in paths if _looks_like_validation_artifact(path)]
    source = [path for path in paths if path not in validation]
    return source, validation


def evaluate_stop(env: Mapping[str, str] | None = None) -> dict:
    values = os.environ if env is None else env
    container_id = str(values.get("OPENHANDS_CONTAINER_ID") or "").strip()
    workspace = str(values.get("OPENHANDS_WORKSPACE") or "/testbed").strip()
    max_rejections = max(
        0, int(values.get("OPENHANDS_EMPTY_PATCH_REJECTIONS") or 0)
    )
    state_path = _state_path(values)
    if not container_id:
        return {
            "decision": "allow",
            "reason": "missing_container_id",
        }
    try:
        patch = _container_patch(container_id, workspace)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {
            "decision": "allow",
            "reason": "patch_guard_error",
            "additionalContext": str(exc),
        }
    source_paths, validation_paths = _source_paths(patch)
    if source_paths:
        _write_state(
            state_path,
            {
                "rejections": _load_rejections(state_path),
                "accepted": True,
                "source_paths": source_paths,
                "validation_paths": validation_paths,
            },
        )
        return {
            "decision": "allow",
            "reason": "source_patch_present",
        }

    rejections = _load_rejections(state_path)
    if rejections >= max_rejections:
        _write_state(
            state_path,
            {
                "rejections": rejections,
                "accepted": False,
                "exhausted": True,
                "validation_paths": validation_paths,
            },
        )
        return {
            "decision": "allow",
            "reason": "empty_patch_rejection_limit_reached",
        }

    rejections += 1
    _write_state(
        state_path,
        {
            "rejections": rejections,
            "accepted": False,
            "exhausted": False,
            "validation_paths": validation_paths,
        },
    )
    detail = (
        "Only validation/test files changed: " + ", ".join(validation_paths)
        if validation_paths
        else "No tracked source file has changed."
    )
    return {
        "decision": "deny",
        "reason": "empty_source_patch",
        "additionalContext": (
            f"{detail} Continue this same task. Re-check the issue and relevant tests, "
            "then make a concrete minimal change to the implementation inside the "
            "existing Docker container. Run git diff --stat in the container and do "
            "not call finish again until at least one non-test tracked source file changed. "
            f"Empty-patch rejection {rejections}/{max_rejections}."
        ),
    }


def main() -> int:
    print(json.dumps(evaluate_stop(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
