#!/usr/bin/env python3
"""Read-only SWE-bench wave status watchdog.

This entry point summarizes configured runs using the pure harness status layer.
It deliberately avoids remote repair, generation starts, and eval starts.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_decision import task_status_row  # noqa: E402
from opencollab.harness.swe_eval_discovery import build_snapshots  # noqa: E402
from opencollab.adapters.safe_files import (  # noqa: E402
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    write_regular_bytes_atomic,
)

MAX_SIDE_NAME_BYTES = 128
MAX_RUNS_CONFIG_BYTES = 16 * 1024 * 1024
MAX_CONFIGURED_RUNS = 4_096
MAX_TASKS_PER_RUN = 10_000
MAX_METADATA_FIELD_BYTES = 4_096
MAX_TASK_ID_BYTES = 512


def _load_runs(path: Path) -> list[dict]:
    try:
        raw = read_regular_bytes(path, max_bytes=MAX_RUNS_CONFIG_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "runs config must be a bounded regular JSON file"
        ) from exc
    if not isinstance(payload, list):
        raise ValueError("runs config must be a JSON list")
    if len(payload) > MAX_CONFIGURED_RUNS:
        raise ValueError(f"runs config exceeds {MAX_CONFIGURED_RUNS} entries")
    return payload


def _safe_component(value: object, *, name: str) -> str:
    text = str(value)
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{name} must be one non-dot path component")
    if text != text.strip():
        raise ValueError(f"{name} must not have leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in text):
        raise ValueError(f"{name} contains an unsafe Unicode character")
    if len(text.encode("utf-8")) > MAX_SIDE_NAME_BYTES:
        raise ValueError(f"{name} exceeds {MAX_SIDE_NAME_BYTES} UTF-8 bytes")
    return text


def _real_absolute_directory(value: object, *, name: str) -> Path:
    raw = str(value)
    if not raw:
        raise ValueError(f"{name} must be a non-empty absolute real directory")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in raw):
        raise ValueError(f"{name} contains an unsafe Unicode character")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute real directory")
    path = Path(os.path.abspath(os.fspath(path)))
    fd = _open_directory_no_symlinks(path)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError(f"{name} must be an absolute real directory")
    finally:
        os.close(fd)
    return path


def _validate_run(
    value: object,
    *,
    index: int,
    default_side_name: str,
) -> tuple[dict | None, list[str]]:
    prefix = f"runs[{index}]"
    if not isinstance(value, dict):
        return None, [f"{prefix} must be a JSON object"]
    errors: list[str] = []
    validated = dict(value)
    try:
        validated["base_run_dir"] = str(
            _real_absolute_directory(
                value.get("base_run_dir"),
                name=f"{prefix}.base_run_dir",
            )
        )
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    try:
        validated["side_name"] = _safe_component(
            value.get("side_name") or default_side_name,
            name=f"{prefix}.side_name",
        )
    except ValueError as exc:
        errors.append(str(exc))
    tasks = value.get("tasks", [])
    if not isinstance(tasks, list) or any(
        not isinstance(task, str) or not task for task in tasks
    ):
        errors.append(f"{prefix}.tasks must be a list of non-empty strings")
    elif len(tasks) > MAX_TASKS_PER_RUN:
        errors.append(f"{prefix}.tasks exceeds {MAX_TASKS_PER_RUN} entries")
    elif len(set(tasks)) != len(tasks):
        errors.append(f"{prefix}.tasks must not contain duplicates")
    elif any(
        len(task.encode("utf-8")) > MAX_TASK_ID_BYTES
        or task != task.strip()
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in task
        )
        for task in tasks
    ):
        errors.append(
            f"{prefix}.tasks contains an unsafe or overlong task id"
        )
    else:
        validated["tasks"] = list(tasks)
    for field in ("name", "workflow", "dataset"):
        if field in value and not isinstance(value[field], str):
            errors.append(f"{prefix}.{field} must be a string")
        elif field in value and len(value[field].encode("utf-8")) > MAX_METADATA_FIELD_BYTES:
            errors.append(
                f"{prefix}.{field} exceeds {MAX_METADATA_FIELD_BYTES} UTF-8 bytes"
            )
        elif field in value and any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value[field]
        ):
            errors.append(f"{prefix}.{field} contains an unsafe Unicode character")
    return (None if errors else validated), errors


def _configured_tasks(run: dict) -> list[str]:
    tasks = run.get("tasks")
    if isinstance(tasks, list):
        return list(tasks)
    return []


def _run_status(run: dict, *, default_side_name: str, allow_advisory_gap: bool) -> dict:
    base = Path(str(run["base_run_dir"]))
    tasks = _configured_tasks(run)
    side_name = str(run.get("side_name") or default_side_name)
    item = {
        "name": str(run.get("name") or base.name or "run"),
        "base_run_dir": str(base),
        "base_exists": True,
        "workflow": str(run.get("workflow") or ""),
        "dataset": str(run.get("dataset") or ""),
        "side_name": side_name,
        "tasks": [],
    }
    snapshots = build_snapshots(base, tasks=tasks, side_name=side_name)
    item["tasks"] = [
        task_status_row(snapshot, allow_advisory_gap=allow_advisory_gap)
        for snapshot in snapshots
    ]
    return item


def _totals(runs: list[dict]) -> dict[str, int]:
    totals = {
        "runs": len(runs),
        "tasks": 0,
        "ready_for_eval": 0,
        "eval_done": 0,
        "technical_eval_failed": 0,
        "empty_patch_invalid": 0,
        "missing_base": 0,
        "invalid_runs": 0,
    }
    for run in runs:
        if run.get("config_errors"):
            totals["invalid_runs"] += 1
        if not run.get("base_exists"):
            totals["missing_base"] += 1
        for task in run.get("tasks") or []:
            totals["tasks"] += 1
            totals["ready_for_eval"] += int(bool(task.get("ready_for_eval")))
            totals["eval_done"] += int(task.get("state") == "eval_done")
            totals["technical_eval_failed"] += int(task.get("state") == "technical_eval_failed")
            totals["empty_patch_invalid"] += int(task.get("state") == "empty_patch_invalid")
    return totals


def build_summary(args: argparse.Namespace) -> dict:
    raw_runs = _load_runs(args.runs_config)
    runs: list[dict] = []
    input_errors: list[str] = []
    for index, raw_run in enumerate(raw_runs):
        run, errors = _validate_run(
            raw_run,
            index=index,
            default_side_name=args.side_name,
        )
        if errors:
            input_errors.extend(errors)
            runs.append(
                {
                    "name": f"invalid-run-{index}",
                    "base_run_dir": "",
                    "base_exists": False,
                    "workflow": "",
                    "dataset": "",
                    "side_name": "",
                    "tasks": [],
                    "config_errors": errors,
                }
            )
            continue
        assert run is not None
        runs.append(
            _run_status(
                run,
                default_side_name=args.side_name,
                allow_advisory_gap=args.eval_advisory_gap,
            )
        )
    return {
        "schema": "opencollab.swe_wave_status.v1",
        "runs_config": str(args.runs_config),
        "side_name": args.side_name,
        "complete": not input_errors,
        "input_errors": input_errors,
        "totals": _totals(runs),
        "runs": runs,
    }


def _write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        ensure_directory_no_symlinks(path.parent)
        write_regular_bytes_atomic(path, text.encode("utf-8"))
    except OSError as exc:
        raise ValueError(f"output path is not a regular file: {path}") from exc


def _write_markdown(path: Path, summary: dict) -> None:
    def cell(value: object) -> str:
        return str(value).replace("\\", "\\\\").replace("|", "\\|")

    totals = summary["totals"]
    lines = [
        "# SWE Wave Status",
        "",
        f"- runs: `{totals['runs']}`",
        f"- tasks: `{totals['tasks']}`",
        f"- ready_for_eval: `{totals['ready_for_eval']}`",
        f"- eval_done: `{totals['eval_done']}`",
        f"- technical_eval_failed: `{totals['technical_eval_failed']}`",
        "",
        "| run | task | state | patch | wf | eval |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for run in summary["runs"]:
        if not run["base_exists"]:
            lines.append(f"| {cell(run['name'])} |  | base_missing |  |  |  |")
            continue
        for task in run["tasks"]:
            eval_summary = task["eval"]
            eval_label = "none"
            if eval_summary["done_count"]:
                eval_label = f"done r={eval_summary['resolved_count']} u={eval_summary['unresolved_count']}"
            elif eval_summary["active_count"]:
                eval_label = "active"
            elif eval_summary["failed_count"]:
                eval_label = "technical_failed"
            lines.append(
                "| {run} | {task} | {state} | {patch} | {wf} | {eval} |".format(
                    run=cell(run["name"]),
                    task=cell(task["task"]),
                    state=cell(task["state"]),
                    patch=cell(task["patch_len"]),
                    wf=cell(task["workflow_status"]),
                    eval=cell(eval_label),
                )
            )
    _atomic_write_text(path, "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize configured SWE-bench wave runs.")
    parser.add_argument("--runs-config", type=Path, required=True)
    parser.add_argument("--side-name", default="official_eval_auto")
    parser.add_argument("--eval-advisory-gap", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    args = parser.parse_args()

    args.runs_config = Path(os.path.abspath(os.fspath(args.runs_config)))
    try:
        args.side_name = _safe_component(args.side_name, name="--side-name")
        summary = build_summary(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json_output:
        _write_json(args.json_output, summary)
    if args.markdown_output:
        _write_markdown(args.markdown_output, summary)
    if not args.json_output and not args.markdown_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
