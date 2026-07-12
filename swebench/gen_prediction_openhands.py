"""Generate a SWE-bench prediction by invoking an external OpenHands command.

The evaluation layer owns dataset slicing, container image selection, patch
extraction, official eval, retries, and reporting. This script only adapts an
OpenHands installation to the same prediction/metrics JSONL shape used by the
other generators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import gen_prediction as gp  # noqa: E402
from gen_prediction_workflow import build_output_records, extract_patch_guarded  # noqa: E402
from gen_prediction_snapshot import (  # noqa: E402
    anonymous_solver_task_id,
    prepare_solver_git_snapshot,
)
from opencollab.adapters.llm.types import Usage  # noqa: E402
from opencollab.adapters.llm.usage_ledger import pricing_for_model, usage_cost_usd  # noqa: E402


DEFAULT_PROMPT = """\
# Issue to fix in `{repo}`

{problem_statement}
{hints_block}
## Workspace
Your terminal is already bound to an isolated, offline workspace at `{workspace}`.
Run repository reads, searches, edits, and tests directly in that terminal. Before
finishing, run `git status --short` and confirm that tracked source files contain
the intended changes.

Fix the source root cause with a minimal patch. Do not edit benchmark tests, do
not run git commit, and leave all source changes in the working tree.
"""


def _prompt(instance: dict, *, container_id: str) -> str:
    hints = str(instance.get("hints_text") or "").strip()
    hints_block = f"\n## Hints\n{hints}\n" if hints else "\n"
    return DEFAULT_PROMPT.format(
        repo=instance.get("repo") or "",
        problem_statement=instance.get("problem_statement") or "",
        hints_block=hints_block,
        container_id=container_id,
        workspace=gp.DOCKER_WORKDIR,
    )


def _template_values(
    *,
    container_id: str,
    instance_id: str,
    instance_file: Path,
    prompt_file: Path,
    output_dir: Path,
    timeout: float,
) -> dict[str, str]:
    raw = {
        "container_id": container_id,
        "workspace": gp.DOCKER_WORKDIR,
        "instance_id": instance_id,
        "instance_file": str(instance_file),
        "prompt_file": str(prompt_file),
        "output_dir": str(output_dir),
        "timeout": str(int(timeout)),
    }
    return {key: shlex.quote(value) for key, value in raw.items()}


def _format_command(template: str, values: dict[str, str]) -> str:
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise SystemExit(f"unknown OpenHands command placeholder: {exc.args[0]}") from exc


def _stop_hook_command() -> str:
    guard_script = shlex.quote(
        str(_REPO_ROOT / "swebench" / "openhands_require_patch.py")
    )
    return (
        f"if [ -f {guard_script} ]; then "
        f"python3.12 {guard_script} || exit 1; "
        "else echo '{\"decision\":\"allow\","
        "\"reason\":\"missing_patch_guard_script\"}'; fi"
    )


def _openhands_usage(output_dir: Path) -> dict[str, int | float] | None:
    state_paths = sorted(
        output_dir.rglob("base_state.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not state_paths:
        return None
    try:
        state = json.loads(state_paths[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stats = state.get("stats") if isinstance(state, dict) else None
    usage_map = stats.get("usage_to_metrics") if isinstance(stats, dict) else None
    if not isinstance(usage_map, dict):
        return None
    totals: dict[str, int | float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "openhands_reported_cost_usd": 0.0,
    }
    for value in usage_map.values():
        if not isinstance(value, dict):
            continue
        accumulated = value.get("accumulated_token_usage")
        if isinstance(accumulated, dict):
            totals["input_tokens"] += int(accumulated.get("prompt_tokens") or 0)
            totals["output_tokens"] += int(accumulated.get("completion_tokens") or 0)
            totals["cache_read_tokens"] += int(accumulated.get("cache_read_tokens") or 0)
            totals["cache_creation_tokens"] += int(
                accumulated.get("cache_write_tokens")
                or accumulated.get("cache_creation_tokens")
                or 0
            )
        totals["openhands_reported_cost_usd"] += float(
            value.get("accumulated_cost") or 0.0
        )
    if not totals["input_tokens"] and not totals["output_tokens"]:
        return None
    totals["total_tokens"] = int(totals["input_tokens"]) + int(totals["output_tokens"])
    return totals


def _append_usage_record(
    *, run_dir: Path, instance_id: str, model: str, usage_values: dict[str, int | float]
) -> dict:
    usage = Usage(
        input_tokens=int(usage_values["input_tokens"]),
        output_tokens=int(usage_values["output_tokens"]),
        cache_read_tokens=int(usage_values["cache_read_tokens"]),
        cache_creation_tokens=int(usage_values["cache_creation_tokens"]),
    )
    pricing = pricing_for_model(model)
    cost_usd = usage_cost_usd(usage, model)
    payload = {
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": max(
            usage.input_tokens - usage.cache_read_tokens - usage.cache_creation_tokens,
            0,
        ),
        "cached_input_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "estimated": False,
        "cost_usd": cost_usd,
        "pricing": pricing,
        "openhands_reported_cost_usd": usage_values["openhands_reported_cost_usd"],
    }
    record = {
        "schema": "opencollab.api_usage.v1",
        "timestamp": time.time(),
        "request_id": str(uuid.uuid4()),
        "status": "success",
        "provider": "openhands",
        "model": model,
        "latency_s": 0.0,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "argv0": Path(sys.argv[0]).name if sys.argv else None,
        "run_id": instance_id,
        "label": "openhands-aggregate",
        "base_url": None,
        "base_url_host": None,
        "usage": payload,
    }
    usage_path = run_dir / "api_usage.jsonl"
    with usage_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return payload


def _run_openhands(
    *,
    command_template: str,
    container_id: str,
    instance: dict,
    instance_file: Path,
    prompt_file: Path,
    output_dir: Path,
    timeout: float,
    context_window: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    token_budget: int | None = None,
    max_steps: int | None = None,
    empty_patch_rejections: int = 0,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _format_command(
        command_template,
        _template_values(
            container_id=container_id,
            instance_id=instance["instance_id"],
            instance_file=instance_file,
            prompt_file=prompt_file,
            output_dir=output_dir,
            timeout=timeout,
        ),
    )
    inherited_names = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "PATH",
        "PYTHONPATH",
        "TERM",
        "TMPDIR",
        "USER",
        "OPENCOLLAB_OPENHANDS_PYTHON",
        "OPENCOLLAB_OPENHANDS_SITE",
        "OPENCOLLAB_PYDEPS",
        "OPENCOLLAB_REMOTE_REPO",
        "OPENCOLLAB_REMOTE_ROOT",
    }
    env = {name: os.environ[name] for name in inherited_names if name in os.environ}
    env.update(
        {
            "OPENHANDS_CONTAINER_ID": container_id,
            "OPENHANDS_WORKSPACE": gp.DOCKER_WORKDIR,
            "OPENHANDS_INSTANCE_ID": instance["instance_id"],
            "OPENHANDS_INSTANCE_FILE": str(instance_file),
            "OPENHANDS_PROMPT_FILE": str(prompt_file),
            "OPENHANDS_OUTPUT_DIR": str(output_dir),
            "OPENHANDS_TIMEOUT": str(int(timeout)),
            "OPENHANDS_PERSISTENCE_DIR": str(output_dir / "persistence"),
            "OPENHANDS_CONVERSATIONS_DIR": str(output_dir / "persistence" / "conversations"),
            "OPENHANDS_WORK_DIR": str(output_dir),
        }
    )
    runtime_values = {
        "OPENHANDS_CONTEXT_WINDOW": context_window,
        "OPENHANDS_TEMPERATURE": temperature,
        "OPENHANDS_TOP_P": top_p,
        "OPENHANDS_MAX_OUTPUT_TOKENS": max_output_tokens,
        "OPENHANDS_TOKEN_BUDGET": token_budget,
        "OPENHANDS_MAX_STEPS": max_steps,
        "OPENHANDS_EMPTY_PATCH_REJECTIONS": empty_patch_rejections,
    }
    env.update(
        {key: str(value) for key, value in runtime_values.items() if value is not None}
    )
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(output_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        status = "done" if returncode == 0 else "openhands_failed"
        if returncode == 0 and any(
            marker in stderr
            for marker in (
                "Traceback (most recent call last)",
                "ModuleNotFoundError:",
                "ImportError:",
            )
        ):
            status = "openhands_failed"
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        status = "openhands_timeout"
    (output_dir / "openhands.command.txt").write_text(command + "\n", encoding="utf-8")
    (output_dir / "openhands.stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
    (output_dir / "openhands.stderr.log").write_text(stderr, encoding="utf-8", errors="replace")
    return {
        "status": status,
        "returncode": returncode,
        "duration_s": round(time.time() - started, 3),
        "command_log": str(output_dir / "openhands.command.txt"),
        "stdout_log": str(output_dir / "openhands.stdout.log"),
        "stderr_log": str(output_dir / "openhands.stderr.log"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one SWE prediction with external OpenHands")
    parser.add_argument("--instance-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metrics")
    parser.add_argument("--image")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--model-name", default="openhands")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--empty-patch-rejections", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=14_400)
    parser.add_argument("--command", default=os.environ.get("OPENCOLLAB_OPENHANDS_COMMAND", ""))
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--dry-run-command", action="store_true")
    args = parser.parse_args()

    if not args.command:
        raise SystemExit(
            "missing OpenHands command. Set OPENCOLLAB_OPENHANDS_COMMAND or pass --command."
        )

    instance_file = Path(args.instance_file)
    instance = json.loads(instance_file.read_text(encoding="utf-8"))
    instance_id = instance["instance_id"]
    solver_task_id = anonymous_solver_task_id()
    image = args.image or f"sweb.eval.{args.arch}.{instance_id}:latest"
    run_dir = Path(args.output).parent
    evidence_dir = run_dir / "openhands_attempts" / solver_task_id
    removed_validation_artifacts: list[str] = []

    name = gp.unique_container_name("oc-oh-", solver_task_id)
    cid = gp.start_container(image, name)
    gp.write_container_marker(run_dir, cid, name)
    openhands_dir = Path(tempfile.mkdtemp(prefix="opencollab-openhands-"))
    print(f"Instance: {instance_id}")
    print(f"Image:    {image}")
    print(f"Container: {cid}")
    try:
        snapshot_evidence = prepare_solver_git_snapshot(cid, instance["base_commit"])
        prompt_file = openhands_dir / "prompt.md"
        openhands_dir.mkdir(parents=True, exist_ok=True)
        solver_instance_file = openhands_dir / "solver_instance.json"
        solver_instance = {
            key: instance[key]
            for key in ("repo", "problem_statement", "hints_text")
            if key in instance
        }
        solver_instance["instance_id"] = solver_task_id
        solver_instance_file.write_text(
            json.dumps(solver_instance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        hooks_dir = openhands_dir / ".openhands"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(
                {
                    "stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _stop_hook_command(),
                                    "timeout": 45,
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        prompt_file.write_text(_prompt(instance, container_id=cid), encoding="utf-8")
        values = _template_values(
            container_id=cid,
            instance_id=solver_task_id,
            instance_file=solver_instance_file,
            prompt_file=prompt_file,
            output_dir=openhands_dir,
            timeout=args.timeout,
        )
        if args.dry_run_command:
            rendered = _format_command(args.command, values)
            print(rendered)
            metrics = {
                "generator": "openhands",
                "solver_git_snapshot": snapshot_evidence.as_dict(),
                "openhands_status": "dry_run",
                "workflow_status": "dry_run",
                "patch_produced": False,
                "submitted_patch_chars": 0,
            }
            patch = ""
        else:
            metrics = {
                "generator": "openhands",
                "solver_git_snapshot": snapshot_evidence.as_dict(),
                **_run_openhands(
                    command_template=args.command,
                    container_id=cid,
                    instance=solver_instance,
                    instance_file=solver_instance_file,
                    prompt_file=prompt_file,
                    output_dir=openhands_dir,
                    timeout=args.timeout,
                    context_window=args.context_window,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    max_output_tokens=args.max_output_tokens,
                    token_budget=args.budget,
                    max_steps=args.max_steps,
                    empty_patch_rejections=max(0, args.empty_patch_rejections),
                ),
            }
            patch, removed_validation_artifacts = extract_patch_guarded(
                cid,
                guard_validation_artifacts=True,
            )
            metrics["patch_produced"] = bool(patch.strip())
            metrics["submitted_patch_chars"] = len(patch)
            if metrics.get("status") == "done":
                metrics["workflow_status"] = (
                    "done" if patch.strip() else "empty_patch_after_done"
                )
            else:
                metrics["workflow_status"] = "error"
            usage_values = _openhands_usage(openhands_dir)
            if usage_values is not None:
                metrics["usage"] = _append_usage_record(
                    run_dir=run_dir,
                    instance_id=instance_id,
                    model=args.llm_model or args.model_name,
                    usage_values=usage_values,
                )
            if removed_validation_artifacts:
                metrics["validation_artifacts_removed"] = removed_validation_artifacts
    finally:
        try:
            evidence_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(openhands_dir, evidence_dir)
        finally:
            shutil.rmtree(openhands_dir, ignore_errors=True)
            if not args.keep_container:
                gp.remove_container_and_clear_marker(run_dir, cid)
            else:
                print(f"  (left container {cid} running: {name})")

    metrics.update(
        {
            "llm_model": args.llm_model or None,
            "context_window": args.context_window,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_output_tokens": args.max_output_tokens,
            "budget": args.budget,
            "max_steps": args.max_steps,
            "empty_patch_rejections": max(0, args.empty_patch_rejections),
            "openhands_empty_patch_rejections": max(
                0, args.empty_patch_rejections
            ),
            "openhands_command_sha256": hashlib.sha256(
                args.command.encode("utf-8")
            ).hexdigest(),
        }
    )
    record, metric_record = build_output_records(
        instance_id=instance_id,
        model_name=args.model_name,
        patch=patch,
        metrics=metrics,
        workflow_name="openhands-external",
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    metrics_path = Path(args.metrics or f"{args.output}.metrics.jsonl")
    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metric_record) + "\n")

    if patch.strip():
        print(f"Patch ({len(patch)} chars) written to {out_path}")
    else:
        print("WARNING: empty patch")


if __name__ == "__main__":
    main()
