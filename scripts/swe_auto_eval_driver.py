#!/usr/bin/env python3
"""Thin SWE-bench evaluation status driver.

The script defaults to read-only status generation. Starting evaluation is a
separate action and requires an explicit command template, keeping classification
logic testable and side-effect free.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_decision import task_status_row  # noqa: E402
from opencollab.harness.swe_eval_discovery import build_snapshots  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, summary: dict) -> None:
    lines = [
        "# SWE Auto Eval Status",
        "",
        f"- run_dir: `{summary['run_dir']}`",
        f"- side_name: `{summary['side_name']}`",
        f"- tasks: `{summary['totals']['tasks']}`",
        f"- ready_for_eval: `{summary['totals']['ready_for_eval']}`",
        f"- eval_done: `{summary['totals']['eval_done']}`",
        f"- technical_eval_failed: `{summary['totals']['technical_eval_failed']}`",
        "",
        "| task | state | patch | wf | eval | reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["tasks"]:
        eval_summary = row["eval"]
        eval_label = "none"
        if eval_summary["active_count"]:
            eval_label = "active"
        elif eval_summary["done_count"]:
            eval_label = f"done r={eval_summary['resolved_count']} u={eval_summary['unresolved_count']}"
        elif eval_summary["failed_count"]:
            eval_label = "technical_failed"
        lines.append(
            "| {task} | {state} | {patch} | {wf} | {eval} | {reason} |".format(
                task=row["task"],
                state=row["state"],
                patch=row["patch_len"],
                wf=row["workflow_status"],
                eval=eval_label,
                reason=row["reason"],
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_summary(args: argparse.Namespace) -> dict:
    active_generation = set(args.active_generation_task or [])
    active_eval = set(args.active_eval_task or [])
    snapshots = build_snapshots(
        args.run_dir,
        tasks=args.task or None,
        side_name=args.side_name,
        active_generation_tasks=active_generation,
        active_eval_tasks=active_eval,
    )
    rows = [task_status_row(snapshot, allow_advisory_gap=args.eval_advisory_gap) for snapshot in snapshots]
    totals = {
        "tasks": len(rows),
        "ready_for_eval": sum(1 for row in rows if row["ready_for_eval"]),
        "eval_done": sum(1 for row in rows if row["state"] == "eval_done"),
        "technical_eval_failed": sum(1 for row in rows if row["state"] == "technical_eval_failed"),
        "empty_patch_invalid": sum(1 for row in rows if row["state"] == "empty_patch_invalid"),
    }
    return {
        "schema": "opencollab.swe_auto_eval_status.v1",
        "run_dir": str(args.run_dir),
        "side_name": args.side_name,
        "start_eval": bool(args.start_eval),
        "totals": totals,
        "tasks": rows,
    }


def _format_eval_command(template: str, row: dict) -> list[str]:
    formatted = template.format(
        task=shlex.quote(row["task"]),
        patch_sha=shlex.quote(row["patch_sha256"]),
        record_id=shlex.quote(row.get("record_id") or ""),
    )
    return shlex.split(formatted)


def maybe_start_eval(args: argparse.Namespace, summary: dict) -> list[dict]:
    if not args.start_eval:
        return []
    if not args.eval_command_template:
        raise SystemExit("--start-eval requires --eval-command-template")
    actions: list[dict] = []
    for row in summary["tasks"]:
        if len(actions) >= args.max_eval_starts:
            break
        if not row["ready_for_eval"]:
            continue
        command = _format_eval_command(args.eval_command_template, row)
        if args.dry_run:
            actions.append({"task": row["task"], "action": "dry_run", "command": command})
            continue
        proc = subprocess.Popen(command, cwd=args.run_dir)
        actions.append({"task": row["task"], "action": "started", "pid": proc.pid, "command": command})
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize and optionally start SWE-bench eval tasks.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--side-name", default="official_eval_auto")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--active-generation-task", action="append", default=[])
    parser.add_argument("--active-eval-task", action="append", default=[])
    parser.add_argument("--eval-advisory-gap", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    parser.add_argument("--start-eval", action="store_true")
    parser.add_argument("--eval-command-template", default="")
    parser.add_argument("--max-eval-starts", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    args.run_dir = args.run_dir.resolve()
    summary = build_summary(args)
    actions = maybe_start_eval(args, summary)
    if actions:
        summary["actions"] = actions
    if args.json_output:
        _write_json(args.json_output, summary)
    if args.markdown_output:
        _write_markdown(args.markdown_output, summary)
    if not args.json_output and not args.markdown_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
