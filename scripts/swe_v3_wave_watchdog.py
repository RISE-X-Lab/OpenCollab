#!/usr/bin/env python3
"""Read-only SWE-bench wave status watchdog.

This entry point summarizes configured runs using the pure harness status layer.
It deliberately avoids remote repair, generation starts, and eval starts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_decision import task_status_row  # noqa: E402
from opencollab.harness.swe_eval_discovery import build_snapshots  # noqa: E402


def _load_runs(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("runs config must be a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _configured_tasks(run: dict) -> list[str]:
    tasks = run.get("tasks")
    if isinstance(tasks, list):
        return [str(task) for task in tasks if str(task)]
    proxies = run.get("remote_proxy_base_urls")
    if isinstance(proxies, dict):
        return [str(task) for task in proxies if str(task)]
    return []


def _run_status(run: dict, *, default_side_name: str, allow_advisory_gap: bool) -> dict:
    base = Path(str(run.get("base_run_dir") or ""))
    tasks = _configured_tasks(run)
    side_name = str(run.get("side_name") or default_side_name)
    item = {
        "name": str(run.get("name") or base.name or "run"),
        "base_run_dir": str(base),
        "base_exists": base.exists(),
        "workflow": str(run.get("workflow") or ""),
        "dataset": str(run.get("dataset") or ""),
        "side_name": side_name,
        "tasks": [],
    }
    if not base.exists():
        return item
    snapshots = build_snapshots(base, tasks=tasks or None, side_name=side_name)
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
    }
    for run in runs:
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
    runs = [
        _run_status(run, default_side_name=args.side_name, allow_advisory_gap=args.eval_advisory_gap)
        for run in _load_runs(args.runs_config)
    ]
    return {
        "schema": "opencollab.swe_wave_status.v1",
        "runs_config": str(args.runs_config),
        "side_name": args.side_name,
        "totals": _totals(runs),
        "runs": runs,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, summary: dict) -> None:
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
            lines.append(f"| {run['name']} |  | base_missing |  |  |  |")
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
                    run=run["name"],
                    task=task["task"],
                    state=task["state"],
                    patch=task["patch_len"],
                    wf=task["workflow_status"],
                    eval=eval_label,
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize configured SWE-bench wave runs.")
    parser.add_argument("--runs-config", type=Path, required=True)
    parser.add_argument("--side-name", default="official_eval_auto")
    parser.add_argument("--eval-advisory-gap", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    args = parser.parse_args()

    args.runs_config = args.runs_config.resolve()
    summary = build_summary(args)
    if args.json_output:
        _write_json(args.json_output, summary)
    if args.markdown_output:
        _write_markdown(args.markdown_output, summary)
    if not args.json_output and not args.markdown_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
