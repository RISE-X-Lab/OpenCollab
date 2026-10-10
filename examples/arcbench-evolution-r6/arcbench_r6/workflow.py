"""The competition harness as a native OC deterministic workflow."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

import yaml
from arc_light.delivery import capture_baseline, load_baseline, preflight
from arc_light.planning import target_rows
from arc_light.public_checks import write_public_checks
from arc_light.reports import write_json
from arc_light.spec import write_cards
from filelock import FileLock

from opencollab import workflow

from .diagnostics import failure_message
from .inputs import _persist_requirement_tree, copy_template_contents_to_output, read_task, resolve_requirements_file
from .prompts import SPEC_DIR
from .runner import EvolutionRunner
from .settings import Settings
from .state import RunState


def emit(data):
    print(json.dumps(data, ensure_ascii=False, default=str), flush=True)


def _initial_status(workspace, run_id):
    path = workspace / ".arc/checks/outcome.json"
    if path.is_file():
        history = workspace / ".arc/history"
        history.mkdir(parents=True, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix="outcome-", dir=history))
        shutil.copy2(path, destination / "outcome.json")
    write_json(path, {"run_id": run_id, "status": "running", "delivery_ok": False, "official_score": None})


async def _prepare_environment(workspace, timeout):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(preflight, workspace, timeout=timeout, cancel_event=cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        await task
        raise


@workflow(
    name="arcbench-evolution-r6",
    description="Independent Single2 instances implement requirement groups, verify and repair a shared application.",
    phases=["prepare", "implement", "verify", "repair", "deliver"],
)
async def evolution(ctx, inputs):
    workspace = Path(ctx.workspace_root).resolve()
    run_id = ctx.run_id or "arc-" + uuid.uuid4().hex
    (workspace / ".arc").mkdir(parents=True, exist_ok=True)
    state = runtime = None
    # This lock protects one mutable application and its reports, not model capacity.
    with FileLock(str(workspace / ".arc/r6-run.lock"), timeout=0):
        _initial_status(workspace, run_id)
        try:
            from arcbench_agent_runtime import AgentRuntime

            if inputs.get("task_type", "web") != "web":
                raise ValueError("The r6 application checks require task_type='web'")
            runtime = AgentRuntime.from_env(project_dir=str(workspace))
            runtime.events.mark_run_started("ARC-Bench r6 workflow started")
            await ctx.phase("Prepare inherited application")
            settings = Settings(**inputs["settings"]) if "settings" in inputs else Settings.from_env()
            requirement_file = resolve_requirements_file(inputs["requirements"])
            task = read_task(requirement_file)
            document = yaml.safe_load(task)
            resume = bool(inputs.get("resume", False))
            state = RunState(workspace, task, run_id=run_id, resume=resume, fresh=bool(inputs.get("fresh", False)))
            if not any((workspace / name / "package.json").exists() for name in ("frontend", "backend")):
                copy_template_contents_to_output(Path(__file__).resolve().parents[1] / "template", workspace)
            if resume:
                load_baseline(workspace, run_id=run_id)
            else:
                capture_baseline(workspace, run_id=run_id, input_source=str(requirement_file))
            index = write_cards(document, workspace, directory_name=SPEC_DIR)
            targets = [row["id"] for row in target_rows(index)]
            if not targets:
                raise ValueError("The supplied document contains no executable requirement leaves")
            write_public_checks(document, workspace, requirement_ids=targets)
            ids = _persist_requirement_tree(runtime, document)
            runtime.git.ensure_repo(create_initial_commit=True)
            for key in ids:
                runtime.events.mark_design_started(key, "Requirement loaded")
                runtime.events.mark_design_done(key, "Requirement card recorded")
                runtime.events.mark_implementation_started(key, "Workflow implementation started")
            # Preparation retains r6's original dependency and browser checks.
            environment = await _prepare_environment(
                workspace,
                max(
                    1,
                    min(600, settings.wall_seconds - state.elapsed - 900),
                ),
            )
            if environment.get("fatal"):
                raise RuntimeError("Environment preflight failed; see preflight.json")
            product = (
                str(document.get("name") or document.get("title") or "ARC-Bench task")
                if isinstance(document, dict)
                else "ARC-Bench task"
            )

            def commit(message):
                try:
                    runtime.git.commit(message)
                except Exception as error:
                    emit({"diag": "commit_failed", "exception_type": type(error).__name__})

            runner = EvolutionRunner(ctx, workspace, index, product, settings, state, commit, emit, resume=resume)
            result = await runner.run()
            commit("r6 workflow delivery and verification")
            await ctx.phase("Delivery result")
            if result["delivery_ok"]:
                runtime.events.mark_run_completed("r6 local delivery checks passed")
            else:
                runtime.events.mark_run_failed("r6 local delivery checks failed or remain unverified")
            return result
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            failure = {
                "run_id": run_id,
                "status": "cancelled" if cancelled else "failed",
                "delivery_ok": False,
                "official_score": None,
                "error_type": type(error).__name__,
                "message": failure_message(error),
            }
            if state is not None:
                failure = state.finish(**{key: value for key, value in failure.items() if key != "run_id"})
            else:
                write_json(workspace / ".arc/checks/outcome.json", failure)
            if runtime is not None:
                runtime.events.mark_run_failed(f"r6 {failure['status']}: {type(error).__name__}")
            if cancelled or not isinstance(error, Exception):
                raise
            emit(failure)
            return failure
