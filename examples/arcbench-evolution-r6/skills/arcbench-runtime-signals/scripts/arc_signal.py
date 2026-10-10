from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_RUNNER_EVENTS_PATH = ".arc/runner-events.jsonl"
DEFAULT_TRACEABILITY_DIR = ".arc/traceability"
DEFAULT_PROJECT_DIR = "."


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _resolve_under_project(project_dir: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_dir / path


def _paths(
    project_dir: str | None,
    events_path: str | None,
    traceability_dir: str | None,
) -> tuple[Path, Path, Path]:
    resolved_project_dir = (
        Path(
            project_dir
            or os.environ.get("ARCBENCH_OUTPUT_DIR", "").strip()
            or os.environ.get("ARCBENCH_PROJECT_DIR", "").strip()
            or os.environ.get("ARCBENCH_TEMPLATE_DIR", "").strip()
            or DEFAULT_PROJECT_DIR
        )
        .expanduser()
        .resolve()
    )
    runner_events_value = (
        events_path or os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip() or DEFAULT_RUNNER_EVENTS_PATH
    )
    traceability_value = (
        traceability_dir or os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or DEFAULT_TRACEABILITY_DIR
    )
    runner_events_path = _resolve_under_project(resolved_project_dir, runner_events_value)
    traceability_path = _resolve_under_project(resolved_project_dir, traceability_value)
    resolved_project_dir.mkdir(parents=True, exist_ok=True)
    runner_events_path.parent.mkdir(parents=True, exist_ok=True)
    traceability_path.mkdir(parents=True, exist_ok=True)
    return resolved_project_dir, runner_events_path, traceability_path


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    tmp_path.replace(path)


def _emit_refresh_signal(
    runner_events_path: Path,
    *,
    reason: str,
    submission: bool = False,
    logs: bool = False,
    commit_history: bool = False,
    traceability_selected: bool = False,
    traceability_all: bool = False,
    preview: bool = False,
) -> None:
    _append_jsonl(
        runner_events_path,
        {
            "type": "signal",
            "reason": str(reason or "").strip() or "arcbench_runtime_signals",
            "timestamp": _utc_timestamp(),
            "refresh": {
                "submission": bool(submission),
                "logs": bool(logs),
                "commit_history": bool(commit_history),
                "traceability_selected": bool(traceability_selected),
                "traceability_all": bool(traceability_all),
                "preview": bool(preview),
            },
        },
    )


def _notify_traceability_changed(runner_events_path: Path, reason: str) -> None:
    _emit_refresh_signal(
        runner_events_path,
        reason=reason,
        submission=True,
        traceability_selected=True,
        traceability_all=True,
    )


def _upsert_node_state(traceability_dir: Path, runner_events_path: Path, req_id: str, state: str, phase: str) -> None:
    normalized_req_id = str(req_id or "").strip()
    if not normalized_req_id:
        return
    table_path = traceability_dir / "node_states.json"
    rows = _read_json(table_path, {})
    rows[normalized_req_id] = {
        "req_id": normalized_req_id,
        "state": str(state or "").strip(),
        "phase": str(phase or "").strip() or None,
        "updated_at": _utc_timestamp(),
    }
    _write_json_atomic(table_path, dict(sorted(rows.items())))
    _notify_traceability_changed(runner_events_path, "node_state_updated")


def _emit_requirement_state(
    runner_events_path: Path,
    traceability_dir: Path,
    *,
    node_id: str,
    phase: str,
    status: str,
    message: str | None = None,
) -> None:
    normalized_node_id = str(node_id or "").strip()
    if not normalized_node_id:
        return
    normalized_phase = str(phase or "").strip()
    normalized_status = str(status or "").strip()
    _append_jsonl(
        runner_events_path,
        {
            "type": "requirement_state",
            "node_id": normalized_node_id,
            "phase": normalized_phase,
            "status": normalized_status,
            "timestamp": _utc_timestamp(),
            "message": message,
        },
    )
    state = {
        ("design", "running"): "DESIGNING",
        ("design", "completed"): "DESIGNED",
        ("design", "failed"): "FAILED",
        ("implement", "running"): "IMPLEMENTING",
        ("implement", "completed"): "IMPLEMENTED",
        ("implement", "failed"): "FAILED",
        ("test", "passed"): "PASSED",
        ("test", "failed"): "FAILED",
    }.get((normalized_phase, normalized_status))
    if state:
        _upsert_node_state(traceability_dir, runner_events_path, normalized_node_id, state, normalized_phase)


def _emit_runner_state(runner_events_path: Path, state: str, message: str | None = None) -> None:
    _append_jsonl(
        runner_events_path,
        {
            "type": "runner_state",
            "state": str(state or "").strip(),
            "timestamp": _utc_timestamp(),
            "message": message,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit ARC Bench runtime events.")
    parser.add_argument("action")
    parser.add_argument("--project-dir")
    parser.add_argument("--events-path")
    parser.add_argument("--traceability-dir")
    parser.add_argument("--node-id")
    parser.add_argument("--message")
    parser.add_argument("--reason", default="agent_signal")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--submission", action="store_true")
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--commit-history", action="store_true")
    parser.add_argument("--traceability-selected", action="store_true")
    parser.add_argument("--traceability-all", action="store_true")
    args = parser.parse_args()

    _, runner_events_path, traceability_dir = _paths(args.project_dir, args.events_path, args.traceability_dir)
    action = args.action.strip().lower()

    lifecycle = {
        "run-started": "running",
        "run-completed": "completed",
        "run-failed": "failed",
        "run-paused": "paused",
        "run-resumed": "resumed",
    }
    node_actions = {
        "design-started": ("design", "running"),
        "design-done": ("design", "completed"),
        "design-failed": ("design", "failed"),
        "implement-started": ("implement", "running"),
        "implement-done": ("implement", "completed"),
        "implement-failed": ("implement", "failed"),
        "test-passed": ("test", "passed"),
        "test-failed": ("test", "failed"),
    }

    if action in lifecycle:
        _emit_runner_state(runner_events_path, lifecycle[action], args.message)
    elif action in node_actions:
        if not args.node_id:
            raise SystemExit("--node-id is required for requirement state actions")
        phase, status = node_actions[action]
        _emit_requirement_state(
            runner_events_path,
            traceability_dir,
            node_id=args.node_id,
            phase=phase,
            status=status,
            message=args.message,
        )
    elif action == "traceability-changed":
        _notify_traceability_changed(runner_events_path, args.reason)
    elif action == "commit-history-changed":
        _emit_refresh_signal(runner_events_path, reason=args.reason, commit_history=True, preview=args.preview)
    elif action == "refresh":
        _emit_refresh_signal(
            runner_events_path,
            reason=args.reason,
            submission=args.submission,
            logs=args.logs,
            commit_history=args.commit_history,
            traceability_selected=args.traceability_selected,
            traceability_all=args.traceability_all,
            preview=args.preview,
        )
    else:
        raise SystemExit(f"Unsupported action: {args.action}")

    print(json.dumps({"ok": True, "action": action, "events_path": str(runner_events_path)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
