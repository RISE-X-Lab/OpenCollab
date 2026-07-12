#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "opencollab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "opencollab"))

from opencollab.harness import token_cost


def _write_outputs(summary: dict[str, Any], json_output: Path | None, markdown_output: Path | None) -> None:
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown_output:
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(token_cost.to_markdown(summary), encoding="utf-8")


def _remote_source() -> str:
    module_source = (REPO_ROOT / "opencollab" / "opencollab" / "harness" / "token_cost.py").read_text(
        encoding="utf-8"
    )
    remote_main = r'''
if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument("--usd-cny", type=float)
    args = parser.parse_args()
    value = build_summary([Path(path) for path in args.run_dir], model_filter=args.model, usd_cny=args.usd_cny)
    print(json.dumps(value, ensure_ascii=False))
'''
    return module_source + "\n" + remote_main


def _loads_summary_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    cursor = 0
    while True:
        start = stdout.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(stdout[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == "opencollab.swe_token_cost_summary.v1"
        ):
            found = value
        cursor = start + max(end, 1)
    if found is not None:
        return found
    raise ValueError("remote stdout did not contain opencollab.swe_token_cost_summary.v1 JSON")


def _build_remote(args: argparse.Namespace) -> dict[str, Any]:
    command = [
        *shlex.split(args.ssh_command),
        args.remote_host,
        "python3",
        "-",
    ]
    for run_dir in args.run_dir:
        command.extend(["--run-dir", run_dir])
    command.extend(["--model", args.model])
    if args.usd_cny is not None:
        command.extend(["--usd-cny", str(args.usd_cny)])
    try:
        proc = subprocess.run(
            command,
            input=_remote_source(),
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_tail = (exc.stdout or "")[-1000:]
        stderr_tail = (exc.stderr or "")[-1000:]
        raise RuntimeError(
            f"remote token summary timed out after {args.timeout}s; "
            f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"ssh exited {proc.returncode}")
    try:
        return _loads_summary_json(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"{exc}; stdout_tail={proc.stdout[-2000:]!r}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize SWE-bench generation token usage and cost.")
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument("--usd-cny", type=float)
    parser.add_argument("--remote-host", default="")
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    if args.remote_host:
        summary = _build_remote(args)
    else:
        summary = token_cost.build_summary(
            [Path(path) for path in args.run_dir],
            model_filter=args.model,
            usd_cny=args.usd_cny,
        )
    _write_outputs(summary, args.json_output, args.markdown_output)
    printed = token_cost.compact_summary(summary) if args.compact else summary
    print(json.dumps(printed, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
