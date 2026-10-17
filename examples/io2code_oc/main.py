"""Reconstruct IO2Code's hidden programs with OpenCollab's built-in ``reconstruct`` workflow.

Run from the OpenCollab repository root:

    export IO2CODE_ROOT=/path/to/IO2Code      # git checkout, see README
    uv run python examples/io2code_oc/main.py Abs_Current Extra_PolishRPN \
        --random-share 0.5 --coverage-gate --docker python:3.12-slim

Each task gets its own git workspace, Docker container (network off) and
result directory under ``--runs``. The oracle function and the 15 held-out
tests stay in this process; the agent only sees the examples the protocol
hands it and the tools in ``io2code_oc/tools.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR.parent))

from io2code_oc.tasks import Task, jsonable, list_tasks, load_task, outputs_match, set_io2code_root  # noqa: E402
from io2code_oc.tools import (  # noqa: E402
    CandidateRunner,
    CheckCandidateTool,
    DockerCandidateRunner,
    QueryLog,
    QueryOracleTool,
)
from io2code_oc.workflow import CANDIDATE_FILE, make_workflow  # noqa: E402
from opencollab import OpenCollab  # noqa: E402
from opencollab.builtin_workflows import ReconstructionTask, reconstruct, register_task  # noqa: E402
from opencollab.environments import docker_environment  # noqa: E402
from opencollab.tools import builtin_tools  # noqa: E402

DEFAULT_IMAGE = "python:3.12-slim"
WORKSPACE_GITIGNORE = ".oc_runner/\n.oc_candidates/\n__pycache__/\n"


def _git_init(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8")
    git = ["git", "-c", "user.name=io2code", "-c", "user.email=io2code@local"]
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run([*git, "add", ".gitignore"], cwd=workspace, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=workspace, check=True)


# Statuses that never produced a scored candidate; a later --resume retries them.
UNFINISHED_STATUSES = frozenset({"crashed", "skipped"})


def placeholder_record(name: str, status: str, reason: str) -> dict[str, Any]:
    """The record for a task that did not reach scoring (crashed or skipped)."""
    return {
        "task": name,
        "status": status,
        "reason": reason,
        "solved": False,
        "held_out_passed": "-",
        "queries": "-",
        "tokens": "-",
        "seconds": "-",
    }


def persist_record(run_dir: Path, record: dict[str, Any]) -> Path:
    """Write ``<run_dir>/<task>/result.json``.

    Every task gets one, including crashed and skipped ones, so a summary
    rebuilt from the run directory (``--resume``) keeps the full denominator.
    """
    task_dir = run_dir / record["task"]
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "result.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def is_finished(record: dict[str, Any]) -> bool:
    return record.get("status") not in UNFINISHED_STATUSES


def make_reconstruction_task(
    task: Task, workspace: Path, runner: CandidateRunner, tools: list[Any], log: QueryLog
) -> ReconstructionTask:
    """Bind one IO2Code task to the built-in workflow."""

    async def evaluate(program: Path, inputs: list[Any]) -> list[dict[str, Any]]:
        return await runner.run(program, task.function_name, inputs)

    def on_example(inp: Any, out: Any, source: str) -> None:
        log.entries.append({"input": inp, "output": out, "source": source})

    return ReconstructionTask(
        input_type=task.input_type,
        input_constraints=task.input_constraints,
        function_name=task.function_name,
        workspace=workspace,
        oracle=task.oracle,
        evaluate=evaluate,
        tools=tools,
        random_inputs=task.random_inputs,
        outputs_match=outputs_match,
        on_example=on_example,
        candidate_file=CANDIDATE_FILE,
        kind=task.kind,
        lo=task.lo,
        hi=task.hi,
        is_float=task.is_float,
    )


async def score_held_out(task: Task, program: Path, runner: CandidateRunner) -> dict[str, Any]:
    """Run the final candidate on the held-out cases exactly as IO2Code scores them (all must pass)."""
    if not program.is_file():
        return {"passed": 0, "total": len(task.held_out), "solved": False, "failures": [f"{CANDIDATE_FILE} missing"]}
    inputs = [inp for inp, _ in task.held_out]
    results = await runner.run(program, task.function_name, inputs)
    failures = []
    for (inp, expected), res in zip(task.held_out, results):
        if not res["ok"]:
            failures.append(f"{inp!r}: expected {expected!r}, got {res['error']}")
        elif not outputs_match(res["output"], jsonable(expected)):
            failures.append(f"{inp!r}: expected {expected!r}, got {res['output']!r}")
    passed = len(task.held_out) - len(failures)
    return {"passed": passed, "total": len(task.held_out), "solved": not failures, "failures": failures}


async def run_task(
    name: str,
    run_dir: Path,
    *,
    mode: str,
    docker_image: str,
    budget: int,
    max_steps: int,
    max_queries: int,
    max_rounds: int,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    task = load_task(name)
    task_dir = run_dir / name
    workspace = task_dir / "ws"
    if task_dir.exists():  # a partial attempt (e.g. interrupted run) starts over cleanly
        shutil.rmtree(task_dir)
    _git_init(workspace)

    log = QueryLog()
    if mode == "fixed":
        # The benchmark's own protocol: the 8 visible examples are the only
        # evidence, so they are preloaded for check_candidate and nobody
        # queries the oracle.
        log.entries = [{"input": i, "output": o} for i, o in task.visible]
        log.preloaded = len(log.entries)

    # The caller owns the Docker environment's lifecycle: the task workspace is
    # bind-mounted into the container (network off) for the whole run, and the
    # same container executes every candidate program.
    environment = docker_environment(docker_image)
    await environment.setup(mount_dir=str(workspace))
    runner = DockerCandidateRunner(environment, workspace)
    tools = [
        *builtin_tools("bash", "file_read", "file_write", "apply_patch"),
        # Only the free mode hands the agent the oracle; in the paper's
        # protocol the workflow queries it, in fixed mode nobody does.
        *([QueryOracleTool(task, log, max_queries)] if mode == "active" else []),
        CheckCandidateTool(task, log, workspace, runner),
    ]
    started = time.time()
    try:
        client = OpenCollab(workspace, environment=environment)
        if mode == "paper-active":
            binding = make_reconstruction_task(task, workspace, runner, tools, log)
            # Workflow arguments are recorded with the artifacts, so the live
            # binding travels through a registry token, not the args dict.
            result = await client.workflow(
                reconstruct,
                {"task": register_task(binding), **protocol},
                budget=budget,
                max_steps=max_steps,
                artifacts=task_dir / "artifacts",
            )
        else:
            flow = make_workflow(
                task, tools, log, workspace, runner, max_queries=max_queries, max_rounds=max_rounds, mode=mode
            )
            result = await client.workflow(flow, budget=budget, max_steps=max_steps, artifacts=task_dir / "artifacts")
        program = workspace / CANDIDATE_FILE
        score = await score_held_out(task, program, runner)
    finally:
        await environment.cleanup()
    elapsed = time.time() - started

    record = {
        "task": name,
        "model": OpenCollab(workspace).configuration.get("model"),
        "mode": mode,
        "protocol": protocol,
        "max_steps": max_steps,
        "status": result.status,
        "reason": result.reason,
        "solved": score["solved"],
        "held_out_passed": f"{score['passed']}/{score['total']}",
        "queries": log.count,
        "tokens": result.tokens,
        "seconds": round(elapsed, 1),
        "workflow": result.output,
        "held_out_failures": score["failures"][:5],
        "candidate": program.read_text(encoding="utf-8") if program.is_file() else None,
    }
    persist_record(run_dir, record)
    (task_dir / "oracle_queries.json").write_text(json.dumps(log.entries, indent=2, default=str), encoding="utf-8")
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tasks", nargs="*", help="IO2Code task names, e.g. Abs_Current (see --list)")
    parser.add_argument("--list", action="store_true", help="print the task names in the checkout and exit")
    parser.add_argument("--io2code", metavar="PATH", default=None, help="IO2Code checkout (default: $IO2CODE_ROOT)")
    parser.add_argument(
        "--runs",
        metavar="DIR",
        default=str(EXAMPLE_DIR / "runs"),
        help="where run directories are created (default: examples/io2code_oc/runs)",
    )
    parser.add_argument("--run-name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument(
        "--docker",
        metavar="IMAGE",
        default=DEFAULT_IMAGE,
        help=f"image for the agent's container and candidate execution (default {DEFAULT_IMAGE})",
    )
    parser.add_argument(
        "--mode",
        choices=("paper-active", "fixed", "active"),
        default="paper-active",
        help="paper-active: the built-in reconstruct workflow (the FromIO paper's protocol); "
        "fixed: only the benchmark's 8 visible examples; active: the agent queries the "
        "oracle freely",
    )
    parser.add_argument("--max-queries", type=int, default=60, help="active mode: oracle query budget")
    parser.add_argument("--rounds", type=int, default=2, help="active/fixed modes: agent rounds per task")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="token budget per task (default 5000000 for paper-active, 150000 otherwise)",
    )
    parser.add_argument("--max-steps", type=int, default=100, help="per agent-session step ceiling")
    parser.add_argument("--concurrency", type=int, default=1, help="tasks to run at the same time")
    parser.add_argument(
        "--resume", action="store_true", help="skip tasks that already have a result.json in this run directory"
    )
    group = parser.add_argument_group("reconstruct workflow disciplines (paper-active mode)")
    group.add_argument(
        "--random-share",
        type=float,
        default=0.0,
        help="fraction of each batch drawn from the benchmark's input generator",
    )
    group.add_argument(
        "--coverage-gate",
        action="store_true",
        help="a promotion counts only once the known inputs cover the structural case categories",
    )
    group.add_argument(
        "--memorisation-gate",
        action="store_true",
        help="a candidate embedding known inputs verbatim is not a verified pass",
    )
    group.add_argument(
        "--complexity-gate", action="store_true", help="a pass by an over-complex candidate needs extra evidence"
    )
    group.add_argument(
        "--domain-check", action="store_true", help="reject agent inputs outside the task's numeric range"
    )
    group.add_argument("--rea", action="store_true", help="shorthand for --random-share 0.5 and all four gates")
    return parser


async def main() -> int:
    args = build_parser().parse_args()
    set_io2code_root(args.io2code)
    if args.list:
        print("\n".join(list_tasks()))
        return 0
    if not args.tasks:
        build_parser().error("give at least one task name (or --list)")
    if args.budget is None:
        args.budget = 5_000_000 if args.mode == "paper-active" else 150_000
    if args.rea:
        args.random_share = args.random_share or 0.5
        args.coverage_gate = args.memorisation_gate = args.complexity_gate = args.domain_check = True
    protocol = {
        "random_share": args.random_share,
        "coverage_gate": args.coverage_gate,
        "memorisation_gate": args.memorisation_gate,
        "complexity_gate": args.complexity_gate,
        "domain_check": args.domain_check,
    }

    run_dir = Path(args.runs).expanduser().resolve() / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    config = OpenCollab(run_dir).configuration
    print(f"model: {config.get('model')} ({config.get('wire_protocol')}) | run dir: {run_dir}", flush=True)

    # Each task owns its workspace, oracle log and container, so tasks can run
    # side by side; the semaphore caps how many at once.
    gate = asyncio.Semaphore(max(1, args.concurrency))
    # A task that "completes" without a single model token means every LLM
    # call failed (bad model config); stop the batch instead of burning
    # through all tasks the same way.
    abort: dict[str, str | None] = {"reason": None}

    async def one(name: str) -> dict[str, Any]:
        done_file = run_dir / name / "result.json"
        if args.resume and done_file.is_file():
            rec = json.loads(done_file.read_text(encoding="utf-8"))
            if is_finished(rec):
                print(
                    f"=== {name}: resumed (solved={rec.get('solved')} held_out={rec.get('held_out_passed')})",
                    flush=True,
                )
                return rec
            print(f"=== {name}: previous attempt {rec.get('status')}, retrying", flush=True)
        async with gate:
            if abort["reason"]:
                rec = placeholder_record(name, "skipped", abort["reason"])
                persist_record(run_dir, rec)
                return rec
            print(f"=== {name} started", flush=True)
            try:
                rec = await run_task(
                    name,
                    run_dir,
                    mode=args.mode,
                    docker_image=args.docker,
                    budget=args.budget,
                    max_steps=args.max_steps,
                    max_queries=args.max_queries,
                    max_rounds=args.rounds,
                    protocol=protocol,
                )
            except Exception as exc:  # noqa: BLE001 - keep the batch going; record the crash
                rec = placeholder_record(name, "crashed", f"{type(exc).__name__}: {exc}")
                persist_record(run_dir, rec)
            print(
                f"=== {name}: {rec['status']} solved={rec['solved']} held_out={rec['held_out_passed']} "
                f"queries={rec['queries']} tokens={rec['tokens']} time={rec['seconds']}s",
                flush=True,
            )
            if rec["status"] == "completed" and not rec.get("tokens"):
                abort["reason"] = (
                    f"{name} completed with zero model tokens; every LLM call failed. "
                    f"See {run_dir / name / 'artifacts'} and fix the model configuration."
                )
                print(f"!!! {abort['reason']}", flush=True)
            return rec

    records = list(await asyncio.gather(*(one(name) for name in args.tasks)))

    # summary.json reflects every result.json in the run directory (crashed
    # and skipped tasks included), so a --resume invocation for a few tasks
    # merges into the earlier batch without shrinking the denominator.
    all_records: dict[str, Any] = {}
    for path in sorted(run_dir.glob("*/result.json")):
        all_records[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    for rec in records:
        all_records.setdefault(rec["task"], rec)
    (run_dir / "summary.json").write_text(
        json.dumps(list(all_records.values()), indent=2, default=str), encoding="utf-8"
    )
    solved = sum(1 for r in records if r["solved"])
    skipped = sum(1 for r in records if r["status"] == "skipped")
    print(
        f"\nsolved {solved}/{len(records)}"
        + (f"  ({skipped} skipped after abort)" if skipped else "")
        + f"  -> {run_dir}"
    )
    return 1 if abort["reason"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
