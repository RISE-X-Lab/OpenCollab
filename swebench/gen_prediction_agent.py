"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from gen_prediction_config import validate_instance_id
from gen_prediction_constants import (
    _ACTIVATE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    DOCKER_WORKDIR,
    MAX_INSTANCE_BYTES,
)
from gen_prediction_constants import (
    REPO_ROOT as _REPO_ROOT,
)
from opencollab.adapters.env import DockerEnvironment
from opencollab.adapters.llm.types import DEFAULT_MAX_OUTPUT_TOKENS
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.trace import Tracer
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.bootstrap.container import agent_save_path, build_session, make_run_dir
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from opencollab.harness.swe_eval_records import read_bounded_json


def build_task(instance: dict) -> str:
    problem = instance["problem_statement"]
    f2p = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    tests = "\n".join(f"- {t}" for t in f2p)
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n\n"
        f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n\n"
        "Locate the root cause in the source, apply a minimal fix, and ensure the "
        "behavior described above is satisfied."
    )


def load_instance(path: str | Path) -> dict:
    document = read_bounded_json(Path(path), max_bytes=MAX_INSTANCE_BYTES)
    if document is None or not isinstance(document[0], dict):
        raise ValueError(f"instance input is not a bounded regular JSON object: {path}")
    instance = document[0]
    instance["instance_id"] = validate_instance_id(instance.get("instance_id"))
    return instance


async def _quiesce_agent_tasks(
    tasks: list[asyncio.Task],
    *,
    grace_seconds: float = AGENT_CANCELLATION_GRACE_SECONDS,
) -> bool:
    """Wait for owned agent work, then repeat cancellation once if needed."""

    async def wait_pending(bound: float) -> set[asyncio.Task]:
        pending = {owned for owned in tasks if not owned.done()}
        if not pending:
            return set()
        _done, pending = await asyncio.wait(pending, timeout=bound)
        return set(pending)

    pending = await wait_pending(grace_seconds)
    if not pending:
        return True
    for owned in pending:
        owned.cancel()
    return not await wait_pending(grace_seconds)


async def run_agent(task: str, cid: str, cfg: dict, max_steps: int, budget: int, timeout: float) -> dict:
    env = DockerEnvironment(
        container_id=cid,
        workspace=DOCKER_WORKDIR,
        exec_workdir=DOCKER_WORKDIR,
        command_prefix=_ACTIVATE,
        timeout_returncode=124,
    )
    agent = Agent(
        name="swe_agent",
        system_prompt=AGENT_PROMPT,
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=cfg.get("temperature", 0.0),
        top_p=cfg.get("top_p"),
        max_tokens_per_step=cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
        thinking=cfg.get("thinking", False),
        thinking_params=cfg.get("thinking_params") or {},
    )
    tracer = Tracer(run_id=f"swe_{uuid.uuid4().hex[:8]}", output_dir=str(_REPO_ROOT / "logs" / "trajectories"))
    session = None
    timed_out = False
    failure: Exception | None = None
    tracer_failure: Exception | None = None
    owned_tasks: list[asyncio.Task] = []
    execution_quiesced = True
    deadline = time.monotonic() + timeout

    async def run_owned(awaitable) -> object:
        owned = asyncio.create_task(awaitable)
        owned_tasks.append(owned)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            owned.cancel()
            raise CallerTimeoutError
        return await abandon_on_timeout(owned, remaining)

    try:
        # Autosave a structured per-agent session JSON under the standard
        # .opencollab/sessions/<timestamp>/ run folder (same convention as team runs).
        run_dir = make_run_dir(str(_REPO_ROOT))
        save_path = agent_save_path(run_dir, 0, agent.name)
        session = build_session(
            agent=agent,
            env=env,
            tracer=tracer,
            max_budget_tokens=budget,
            max_steps=max_steps,
            auto_save_path=save_path,
        )
        print(f"  session autosave: {save_path}")
        await run_owned(session.add_user_message(task))
        await run_owned(session.run_loop())
    except CallerTimeoutError:
        timed_out = True
        print("  agent: wall-clock timeout reached, capturing current diff")
    except Exception as exc:  # preserve a partial worktree as a failed candidate
        failure = exc
        print(f"  agent: failed with {type(exc).__name__}: {exc}")
    finally:
        execution_quiesced = await _quiesce_agent_tasks(
            owned_tasks,
            grace_seconds=AGENT_CANCELLATION_GRACE_SECONDS,
        )
        if not execution_quiesced:
            await env.abort()
        try:
            tracer.close()
        except Exception as exc:  # preserve the candidate and expose trace loss
            tracer_failure = exc
        if tracer_failure is None and getattr(tracer, "write_error", None):
            tracer_failure = OSError(f"trajectory write failed: {tracer.write_error}")
    step_count = int(getattr(session, "step_count", 0))
    used_tokens = int(getattr(session, "used_tokens", 0))
    print(f"  agent: steps={step_count} tokens={used_tokens}")
    phase = getattr(session, "phase", None)
    phase_value = phase.value if isinstance(phase, SessionPhase) else "error"
    if timed_out and execution_quiesced:
        workflow_status = "done_with_timeout_patch"
    elif not execution_quiesced:
        workflow_status = "error"
    elif failure is not None:
        workflow_status = phase_value if phase is not None and phase.is_terminal() else "error"
    elif phase is SessionPhase.DONE:
        workflow_status = "done"
    else:
        workflow_status = phase_value
    metrics = {
        "workflow_status": workflow_status,
        "session_phase": phase_value,
        "step_count": step_count,
        "used_tokens": used_tokens,
        "wall_clock_timeout": timed_out,
        "execution_quiesced": execution_quiesced,
        "submission_eligible": execution_quiesced and workflow_status in {"done", "done_with_timeout_patch"},
    }
    if not execution_quiesced:
        metrics["error_type"] = "ExecutionNotQuiesced"
        metrics["error"] = "agent execution remained active after bounded cancellation cleanup"
    if failure is not None:
        metrics["error_type"] = type(failure).__name__
        metrics["error"] = str(failure)
    if tracer_failure is not None:
        metrics["tracer_close_error_type"] = type(tracer_failure).__name__
        metrics["tracer_close_error"] = str(tracer_failure)
    if getattr(tracer, "write_error", None):
        metrics["tracer_write_error"] = str(tracer.write_error)
    metrics["tracer_dropped_steps"] = int(getattr(tracer, "dropped_steps", 0))
    return metrics
