#!/usr/bin/env python3
"""Run the reusable collaborating team (`configs/team.collab.yaml`) on a workspace.

The team file needs a prebuilt roster: all three roles seated before the first
model call, and no `spawn_agent` anywhere. The `opencollab` CLI has no flag for
that, so this script is the script route -- it passes `prebuild_team=True` and
cannot be pointed at a run that would silently seat the Analyst alone.

    scripts/run_collab_team.py --workspace ./repo --prompt "fix the failing test"
    scripts/run_collab_team.py --workspace ./repo --prompt-file issue.md --budget 2000000

Everything else (model, provider, key, base URL) comes from the usual
environment/config resolution; --model overrides it for one run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencollab import OpenCollab  # noqa: E402
from opencollab.sdk.result import RunResult  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "configs" / "team.collab.yaml"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, help="directory the team works in")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="the task, inline")
    source.add_argument("--prompt-file", help="the task, read from a file")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="team file")
    parser.add_argument("--budget", type=int, default=1_000_000, help="token pool")
    parser.add_argument("--timeout", type=float, default=None, help="seconds, whole run")
    parser.add_argument("--max-steps", type=int, default=100, help="step ceiling per seat")
    parser.add_argument("--model", default=None)
    parser.add_argument("--artifacts", default=None, help="directory for the trajectory")
    parser.add_argument(
        "--no-worktrees",
        action="store_true",
        help="share one directory instead of giving each role its own git worktree",
    )
    parser.add_argument(
        "--concurrent",
        action="store_true",
        help="let woken teammates run beside the current turn (default: one at a time)",
    )
    return parser.parse_args(argv)


def _task_text(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    return Path(args.prompt_file).read_text(encoding="utf-8")


def _handoff_summary(result: RunResult[str]) -> str:
    """One line of what the roster actually did, from the run's own metrics."""
    summary = (result.metrics or {}).get("run_summary") or {}
    steps = summary.get("steps")
    if isinstance(steps, dict) and steps:
        seats = ", ".join(f"{name}={count}" for name, count in sorted(steps.items()))
    else:
        seats = "unavailable"
    return f"seats: {seats}"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = OpenCollab(args.workspace, model=args.model)
    result = asyncio.run(
        client.team(
            _task_text(args),
            config=args.config,
            budget=args.budget,
            timeout=args.timeout,
            max_steps=args.max_steps,
            artifacts=args.artifacts,
            # The team file states these three as facts about the run, so the
            # script sets them rather than exposing them: the roster is seated
            # up front (no role holds `spawn_agent`), and every seat is a real
            # git worktree unless the caller asks otherwise.
            prebuild_team=True,
            use_worktrees=not args.no_worktrees,
            serialize_turns=not args.concurrent,
        )
    )
    print(result.output or "")
    print(
        f"\n-- status={result.status} reason={result.reason} "
        f"tokens={result.tokens} {_handoff_summary(result)}",
        file=sys.stderr,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
