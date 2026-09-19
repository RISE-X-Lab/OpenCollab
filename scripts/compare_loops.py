"""Run the same tasks through the thin loop and the main loop and record what each did.

    uv run python scripts/compare_loops.py --tasks scripts/compare_tasks_r3.json \\
        --loops thin,main --runs 3 --out /tmp/compare_results_r3.jsonl

Every (task, run, loop) executes in a child process with a hard timeout, against
the workspace this script runs in. Before each run the working tree must be
clean; after each run the tree is restored (``git checkout -- .`` plus
``git clean -fd``) and the evidence the judgement needs (git status, diff, new
files, verification commands) is saved under the run directory. Both loops get
the same model configuration (``configs/.env``), the same tool preset, the same
system prompt, and the same limits. One JSON line per run goes to ``--out``.

Task file entries::

    {"id": "T01", "prompt": "...", "target_layer": "baseline",
     "tools": "coding" | "read" | ["coding", "submit_findings"],
     "main_enforcement": "off" | "on",
     "token_limit": 300000, "step_limit": 100, "timeout": 600, "branch": null,
     "check": {"kind": "expected_text" | "command" | "stop_reason" | "artifact" | "key_recall", ...}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = (
    "You are a software engineer working inside the workspace at {workspace}. All paths are "
    "relative to it; do not search outside it. Use the tools to inspect and change files. When "
    "the task is finished, reply with a plain-text summary of what you did and what you "
    "verified, and make no tool call."
)
WRITE_TOOLS = {"file_write", "apply_patch"}
DEFAULTS = {"token_limit": 300_000, "step_limit": 100, "timeout": 600.0}


# --------------------------------------------------------------------------- child
def _tools(spec: Any) -> list[Any]:
    from opencollab.application.submit_findings import SubmitFindingsTool
    from opencollab.bootstrap.programmatic import resolve_tools

    names = [spec] if isinstance(spec, str) else list(spec)
    tools: list[Any] = []
    for name in names:
        if name == "submit_findings":
            tools.append(SubmitFindingsTool())
        else:
            tools.extend(resolve_tools(name))
    return tools


def _agent(cfg: Any, prompt: str, tools: list[Any]):
    from opencollab.domain.agent import Agent

    return Agent(
        name="solo",
        system_prompt=prompt,
        tools=tools,
        model=cfg.model,
        provider=cfg.provider,
        wire_protocol=cfg.wire_protocol,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        max_tokens_per_step=cfg.max_output_tokens,
        context_window=cfg.context_window,
        thinking=cfg.thinking,
        thinking_params=cfg.thinking_params,
        reasoning_effort=cfg.reasoning_effort,
    )


def _llm(cfg: Any):
    from opencollab.adapters.llm import LLMClient

    return LLMClient(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        provider=cfg.provider,
        wire_protocol=cfg.wire_protocol,
        max_retries=cfg.llm_max_retries,
        request_timeout=cfg.llm_timeout,
        connect_timeout=cfg.llm_connect_timeout,
        first_event_timeout=cfg.llm_first_event_timeout,
        stream_idle_timeout=cfg.llm_stream_idle_timeout,
        context_window=cfg.context_window,
        provider_error_time_budget=cfg.provider_error_time_budget,
    )


async def _child_thin(task: dict, run_dir: pathlib.Path, prompt: str, cfg: Any) -> dict:
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.adapters.trace import Tracer
    from opencollab.application.thin_run import ThinRun
    from opencollab.bootstrap.runtime_context import build_workspace_safety_policy

    environment = LocalEnvironment(str(REPO))
    llm = _llm(cfg)
    tracer = Tracer(run_id="thin", output_dir=str(run_dir), filename="trajectory.jsonl")
    loop = ThinRun(
        llm,
        _agent(cfg, prompt, _tools(task["tools"])),
        environment,
        step_limit=task["step_limit"],
        token_limit=task["token_limit"],
        tracer=tracer,
    )
    loop.tools.safety_policy = build_workspace_safety_policy(environment)
    start = time.monotonic()
    crash = None
    try:
        reason, final_text = await asyncio.wait_for(loop.run(task["prompt"]), timeout=task["timeout"])
    except asyncio.TimeoutError:
        reason, final_text = "timeout", ""
    except Exception as exc:  # noqa: BLE001 - a crash is an outcome to record
        reason, final_text, crash = f"crashed: {type(exc).__name__}: {exc}", "", traceback.format_exc()
    finally:
        seconds = time.monotonic() - start
        await environment.cleanup()
        close = getattr(llm, "close", None)
        if callable(close):
            await close()
        tracer.flush()
        (run_dir / "messages.json").write_text(json.dumps(loop.messages, indent=1, ensure_ascii=False))
    return {
        "steps": loop.steps,
        "tokens": loop.tokens,
        "seconds": round(seconds, 1),
        "reason": reason,
        "final_text": final_text,
        "crash": crash,
    }


async def _child_main(task: dict, run_dir: pathlib.Path, prompt: str, cfg: Any) -> dict:
    """The main loop via ``run_agent``; with enforcement on, via ``build_session`` so the
    knob can be set (``run_agent`` does not expose it). Both are bootstrap entry points."""
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.adapters.trace import Tracer
    from opencollab.application._session_run_shared import ENFORCEMENT_ON
    from opencollab.bootstrap import build_session
    from opencollab.bootstrap.programmatic import run_agent
    from opencollab.bootstrap.runtime_context import build_workspace_safety_policy

    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    tools = _tools(task["tools"])
    start = time.monotonic()
    try:
        if task["main_enforcement"] == "on":
            environment = LocalEnvironment(str(REPO))
            tracer = Tracer(run_id="main", output_dir=str(artifacts), filename="trajectory.jsonl")
            session = build_session(
                agent=_agent(cfg, prompt, tools),
                env=environment,
                tracer=tracer,
                max_budget_tokens=task["token_limit"],
                max_steps=task["step_limit"],
                auto_save_path=str(artifacts / "agent.json"),
                safety_policy=build_workspace_safety_policy(environment),
                llm_timeout=cfg.llm_timeout,
            )
            session.runner.configure_enforcement(enforcement_strength=ENFORCEMENT_ON)
            try:
                await session.add_user_message(task["prompt"])
                try:
                    answer = await asyncio.wait_for(session.run_loop(), timeout=task["timeout"])
                    reason = (
                        "completed"
                        if session.state.phase.value == "done"
                        else (session.state.terminal_reason or session.state.phase.value)
                    )
                except asyncio.TimeoutError:
                    answer, reason = "", "timeout"
            finally:
                tracer.flush()
                await session.aclose()
                await environment.cleanup()
            return {
                "steps": session.state.step_count,
                "tokens": session.state.used_tokens,
                "seconds": round(time.monotonic() - start, 1),
                "reason": reason,
                "final_text": answer or "",
                "crash": None,
            }
        result = await run_agent(
            prompt=task["prompt"],
            config=cfg.model_dump(),
            workspace=str(REPO),
            tools=tools,
            max_tokens=task["token_limit"],
            max_steps=task["step_limit"],
            timeout=task["timeout"],
            cleanup_timeout=10.0,
            artifacts=artifacts,
            trace=True,
            system_prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001 - a crash is an outcome to record
        return {
            "steps": None,
            "tokens": None,
            "seconds": round(time.monotonic() - start, 1),
            "reason": f"crashed: {type(exc).__name__}: {exc}",
            "final_text": "",
            "crash": traceback.format_exc(),
        }
    return {
        "steps": result.metrics.get("steps"),
        "tokens": result.tokens,
        "seconds": round(time.monotonic() - start, 1),
        "reason": "completed" if result.status == "completed" else (result.reason or result.status),
        "final_text": result.output or "",
        "crash": None,
    }


async def child(args: argparse.Namespace) -> int:
    from opencollab.bootstrap.config import build_config

    task = json.loads(pathlib.Path(args.task_file).read_text())
    run_dir = pathlib.Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = build_config(str(REPO))
    prompt = SYSTEM_PROMPT.format(workspace=REPO)
    record = await (_child_thin if args.loop == "thin" else _child_main)(task, run_dir, prompt, cfg)
    if record.get("crash"):
        (run_dir / "crash.txt").write_text(record["crash"])
    record.pop("crash", None)
    (run_dir / "result.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))
    print("RESULT " + json.dumps(record, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- parent
def sh(cmd: str, timeout: float = 600) -> str:
    proc = subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    return (proc.stdout + proc.stderr).strip()


def dirty_paths() -> list[str]:
    return [
        line
        for line in sh("git status --porcelain").splitlines()
        if not line.endswith(".idea/") and "configs/.env" not in line
    ]


def restore(original_branch: str) -> None:
    sh("git checkout -- . && git clean -fd -e .idea -e configs/.env")
    if sh("git branch --show-current") != original_branch:
        sh(f"git checkout -q {original_branch}")


def trajectory(run_dir: pathlib.Path, loop: str) -> list[dict]:
    path = run_dir / ("trajectory.jsonl" if loop == "thin" else "artifacts/trajectory.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _call_names(row: dict) -> list[str]:
    return [
        ((c.get("function") or {}).get("name") or c.get("name") or "") for c in (row["payload"].get("tool_calls") or [])
    ]


def layer_metrics(rows: list[dict], loop: str) -> dict:
    llm = [r for r in rows if r["type"] == "llm_call"]
    first_write = next((i + 1 for i, r in enumerate(llm) if WRITE_TOOLS & set(_call_names(r))), None)
    nudges = [r for r in rows if r["type"] == "steering_nudge"]
    soft = next((r["payload"].get("step") for r in nudges if r["payload"].get("level") == "soft"), None)
    hard = next((r["payload"].get("step") for r in nudges if r["payload"].get("level") == "hard"), None)
    compaction = Counter(
        r["payload"].get("rung") for r in rows if r["type"] == "context_shaping" and r["payload"].get("rung") != "none"
    )
    enforcement = Counter(
        r["type"]
        for r in rows
        if r["type"] in ("commit_brake", "budget_reserve_allocated", "empty_stop_retry", "required_tool_retry")
    )
    loop_hits = sum(1 for r in rows if r["type"] == "loop_blocked")
    if loop == "thin":
        calls = Counter(
            (r["payload"].get("tool"), json.dumps(r["payload"].get("args"), sort_keys=True))
            for r in rows
            if r["type"] == "tool_exec"
        )
        loop_hits = sum(1 for _k, n in calls.items() if n >= 3)
    return {
        "first_write_step": first_write,
        "nudge_soft_step": soft,
        "nudge_hard_step": hard,
        "compaction_events": dict(compaction),
        "enforcement_events": dict(enforcement),
        "loop_block_hits": loop_hits,
        "max_call_tokens": max((r["metrics"].get("tokens", 0) for r in llm), default=0),
        "tool_calls": sum(len(_call_names(r)) for r in llm),
    }


def save_evidence(task: dict, run_dir: pathlib.Path) -> tuple[list[str], dict[str, str]]:
    changed = dirty_paths()
    parts = ["# git status --porcelain", *changed, "", "# git diff", sh("git diff")]
    for line in changed:
        if line.startswith("??"):
            path = REPO / line[3:].strip()
            if path.is_file() and path.stat().st_size < 20_000:
                parts += ["", f"# new file: {line[3:].strip()}", path.read_text(errors="replace")]
    outputs: dict[str, str] = {}
    for cmd in task.get("check", {}).get("commands", []):
        outputs[cmd] = sh(cmd)
        parts += ["", f"# verify: {cmd}", outputs[cmd]]
    (run_dir / "evidence.txt").write_text("\n".join(parts))
    return changed, outputs


def judge(task: dict, record: dict, changed: list[str], outputs: dict[str, str]) -> tuple[bool | None, str]:
    """Automatic verdict where the check allows one; ``None`` means a human judges it."""
    check = task.get("check", {})
    kind = check.get("kind")
    text = (record.get("final_text") or "").lower()
    if record.get("reason", "").startswith("crashed"):
        return False, "crashed"
    if check.get("git_clean") and changed:
        return False, f"tree changed: {changed}"
    if kind == "stop_reason":
        hits = [s for s in check["contains_any"] if s in record.get("reason", "")]
        return bool(hits), f"reason={record.get('reason')!r}"
    if kind == "command":
        misses = [
            f"{i}:{needle}"
            for i, needle in check.get("expect", [])
            if needle not in outputs.get(check["commands"][i], "")
        ]
        clean_ok = not check.get("git_clean") or not changed
        return (not misses and clean_ok), (f"missing {misses}" if misses else "") + (
            "" if clean_ok else " tree not clean"
        )
    if kind == "artifact":
        ok, notes = True, []
        if check.get("git_clean") and changed:
            ok, notes = False, notes + [f"changed {changed}"]
        if "only_files" in check:
            extra = [c for c in changed if c[3:].strip() not in check["only_files"]]
            if extra:
                ok, notes = False, notes + [f"unexpected {extra}"]
        for i, needle in check.get("expect", []):
            if needle not in outputs.get(check["commands"][i], ""):
                ok, notes = False, notes + [f"missing {i}:{needle}"]
        if "max_changed_lines" in check:
            numstat = sh("git diff --numstat")
            total = sum(int(a) + int(b) for a, b, _ in (line.split("\t") for line in numstat.splitlines() if line))
            if total > check["max_changed_lines"]:
                ok, notes = False, notes + [f"{total} changed lines"]
        return ok, "; ".join(notes) or "ok"
    if kind == "key_recall":
        items = check["items"]
        hit = sum(1 for item in items if all(str(part).lower() in text for part in item))
        ratio = hit / len(items) if items else 0.0
        verdict = ratio >= check.get("min_ratio", 0.9)
        return verdict, f"recall {hit}/{len(items)}"
    if kind == "expected_text":
        needed = [s for s in check.get("expect_all", []) if s.lower() not in text]
        any_ok = not check.get("expect_any") or any(s.lower() in text for s in check["expect_any"])
        if not needed and any_ok and not check.get("manual"):
            return True, "all expected substrings present"
        if needed or not any_ok:
            return (False if check.get("strict") else None), f"missing {needed}" + (
                "" if any_ok else " none of expect_any"
            )
        return None, "needs a human judgement"
    return None, "no automatic check"


def run_one(
    task: dict, run_index: int, loop: str, run_root: pathlib.Path, out: pathlib.Path, original_branch: str
) -> dict:
    run_dir = run_root / task["id"] / f"run{run_index}" / loop
    if run_dir.exists():
        raise SystemExit(f"run dir exists: {run_dir}")
    run_dir.mkdir(parents=True)
    if dirty_paths():
        raise SystemExit(f"tree not clean before {task['id']} run{run_index} {loop}: {dirty_paths()}")
    if task.get("branch"):
        sh(f"git checkout -q {task['branch']}")
    task_file = run_dir / "task.json"
    task_file.write_text(json.dumps(task))
    cmd = [
        "uv",
        "run",
        "python",
        str(pathlib.Path(__file__).resolve()),
        "--child",
        "--loop",
        loop,
        "--task-file",
        str(task_file),
        "--run-dir",
        str(run_dir),
    ]
    started = time.monotonic()
    record: dict[str, Any]
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=task["timeout"] + 120)
        (run_dir / "stdout.txt").write_text(proc.stdout)
        (run_dir / "stderr.txt").write_text(proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
        if lines:
            record = json.loads(lines[-1][len("RESULT ") :])
        else:
            record = {
                "steps": None,
                "tokens": None,
                "seconds": round(time.monotonic() - started, 1),
                "reason": f"crashed: child exit {proc.returncode}",
                "final_text": "",
            }
    except subprocess.TimeoutExpired as exc:
        (run_dir / "stdout.txt").write_text(exc.stdout or "")
        record = {
            "steps": None,
            "tokens": None,
            "seconds": round(time.monotonic() - started, 1),
            "reason": "timeout",
            "final_text": "",
        }
    changed, outputs = save_evidence(task, run_dir)
    correct, basis = judge(task, record, changed, outputs)
    metrics = layer_metrics(trajectory(run_dir, loop), loop)
    restore(original_branch)
    if dirty_paths():
        raise SystemExit(f"tree not clean after restore for {task['id']} run{run_index} {loop}: {dirty_paths()}")
    row = {
        "loop": loop,
        "task": task["id"],
        "run": run_index,
        "target_layer": task.get("target_layer"),
        "correct": correct,
        "basis": basis,
        **{k: record.get(k) for k in ("steps", "tokens", "seconds", "reason")},
        **metrics,
        "files_changed": changed,
        "final_text": record.get("final_text", ""),
        "run_dir": str(run_dir),
    }
    with out.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def parent(args: argparse.Namespace) -> int:
    tasks = json.loads(pathlib.Path(args.tasks).read_text())
    only = set(args.only.split(",")) if args.only else None
    loops = args.loops.split(",")
    out_path = pathlib.Path(args.out)
    run_root = pathlib.Path(args.run_root) if args.run_root else out_path.with_name(out_path.stem + "_runs")
    original_branch = sh("git branch --show-current")
    for task in tasks:
        if only and task["id"] not in only:
            continue
        for key, value in DEFAULTS.items():
            task.setdefault(key, value)
        task.setdefault("main_enforcement", "off")
        task.setdefault("tools", "coding")
        for run_index in range(1, args.runs + 1):
            for loop in loops:
                row = run_one(task, run_index, loop, run_root, pathlib.Path(args.out), original_branch)
                summary = {
                    k: row[k]
                    for k in (
                        "correct",
                        "steps",
                        "tokens",
                        "seconds",
                        "reason",
                        "first_write_step",
                        "nudge_soft_step",
                        "nudge_hard_step",
                        "compaction_events",
                        "enforcement_events",
                        "loop_block_hits",
                    )
                }
                print(f"{task['id']} run{run_index} {loop:4} {summary}", flush=True)
    return 0


def summarize(args: argparse.Namespace) -> int:
    """Median steps/tokens and correct counts per task and loop, from an ``--out`` file."""
    rows = [json.loads(line) for line in pathlib.Path(args.out).read_text().splitlines() if line.strip()]
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["task"], row["loop"]), []).append(row)
    for (task, loop), items in sorted(groups.items()):
        steps = [r["steps"] for r in items if r["steps"] is not None]
        tokens = [r["tokens"] for r in items if r["tokens"] is not None]
        correct = sum(1 for r in items if r["correct"] is True)
        pending = sum(1 for r in items if r["correct"] is None)
        manual = f" (+{pending} manual)" if pending else ""
        med = lambda xs: statistics.median(xs) if xs else "-"  # noqa: E731
        span = f"{min(tokens)}-{max(tokens)}" if tokens else "-"
        reasons = Counter(r["reason"][:40] for r in items)
        print(
            print(
                f"{task} {loop:4} correct={correct}/{len(items)}{manual} steps med={med(steps)} "
                f"tokens med={med(tokens)} range={span} reasons={dict(reasons)}"
            )
        )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=str(REPO / "scripts" / "compare_tasks_r3.json"))
    parser.add_argument("--loops", default="thin,main")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", default=None, help="comma-separated task ids")
    parser.add_argument("--out", default="/tmp/compare_results.jsonl")
    parser.add_argument("--run-root", default=None)
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--loop", choices=["thin", "main"])
    parser.add_argument("--task-file")
    parser.add_argument("--run-dir")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.child:
        return asyncio.run(child(args))
    if args.summarize:
        return summarize(args)
    return parent(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
