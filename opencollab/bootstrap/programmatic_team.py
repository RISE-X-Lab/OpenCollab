"""Team-mode composition entry point.

Kept separate from the single-agent/workflow composition helpers so the
composition root stays below the repository's module-size ceiling.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from opencollab.adapters.trace import Tracer
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.bootstrap import programmatic as _programmatic
from opencollab.bootstrap.programmatic import (
    DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS,
    ProgrammaticLifecycleError,
    ProgrammaticResult,
)
from opencollab.bootstrap.runtime_context import build_runtime_context
from opencollab.bootstrap.team_config import load_team_config
from opencollab.domain.session import SessionPhase


async def run_team(
    *,
    prompt: str,
    config: Mapping[str, Any],
    workspace: str,
    team_config_path: str | os.PathLike[str] | None,
    max_tokens: int,
    timeout: float | None,
    cleanup_timeout: float = DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS,
    artifacts: Path | None,
    trace: bool,
    use_worktrees: bool,
    prebuild_team: bool = False,
    allow_unisolated_shell: bool | None = None,
) -> ProgrammaticResult:
    """Run the scheduler regime once, including bounded team cleanup.

    ``prebuild_team`` and ``allow_unisolated_shell`` are handed straight to
    ``build_scheduler``; see its docstring for what each decides. Both default
    to the values that reproduce today's run: no roster is seated up front, and
    the shell answer still follows ``interactive``, which is ``False`` here
    because a programmatic run has no human at it. Stating them is how an
    unattended experiment gets a declared roster whose agents can run ``git``
    without also being handed an ``ask_user`` there is nobody to answer.
    """
    run_config = dict(config)
    run_config["budget"] = max_tokens
    context = build_runtime_context(workspace, run_config, trace=False)
    team_config = load_team_config(workspace, path=team_config_path)
    _programmatic._claim_artifacts(artifacts)
    if artifacts is not None and trace:
        context.tracer = Tracer(
            run_id="team",
            output_dir=str(artifacts),
            filename="trajectory.jsonl",
        )
    try:
        scheduler = _programmatic.build_scheduler(
            context,
            use_worktrees=use_worktrees,
            # No human is at a programmatic run, so nobody can answer
            # ``ask_user``. Whether an agent may open an unsandboxed shell is a
            # separate question, and the caller answers it.
            interactive=False,
            allow_unisolated_shell=allow_unisolated_shell,
            auto_save=artifacts is not None,
            team_config_path=team_config_path,
            resolved_team_config=team_config,
            save_dir=artifacts,
            prebuild_team=prebuild_team,
        )
    except BaseException as exc:
        tracer_failure = _programmatic._close_tracer(context.tracer)
        if tracer_failure is not None:
            add_exception_note(
                exc,
                "team tracer close also failed: "
                f"{type(tracer_failure).__name__}: {tracer_failure}",
            )
        raise
    output: str | None = None
    status: Literal["completed", "stopped", "failed"] = "completed"
    reason: str | None = None
    failure: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None
    try:
        try:
            if timeout is None:
                output = await scheduler.run(prompt)
            else:
                output = await asyncio.wait_for(scheduler.run(prompt), timeout=timeout)
        except TimeoutError as exc:
            status = "stopped"
            reason = "timeout"
            failure = exc
        except SchedulerTurnError as exc:
            output = exc.partial_answer
            status = (
                "stopped"
                if exc.phase is SessionPhase.STOPPED
                else "failed"
            )
            reason = exc.terminal_reason or exc.phase.value
            failure = exc
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception as exc:
            status = "failed"
            reason = str(exc) or type(exc).__name__
            failure = exc
        if status == "completed":
            lead = scheduler.lead_session
            phase = getattr(getattr(lead, "phase", None), "value", None)
            terminal_reason = getattr(getattr(lead, "state", None), "terminal_reason", None)
            if phase == "stopped":
                status = "stopped"
                reason = terminal_reason
            elif phase == "error":
                status = "failed"
                reason = terminal_reason or "team failed"
    finally:
        cleanup_failure: BaseException | None = None
        try:
            await scheduler.cleanup(cleanup_timeout=cleanup_timeout)
        except BaseException as exc:
            cleanup_failure = exc
        tracer_failure = _programmatic._close_tracer(context.tracer)
        if cancellation is not None:
            if cleanup_failure is not None:
                add_exception_note(
                    cancellation,
                    "team cleanup also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            if tracer_failure is not None:
                add_exception_note(
                    cancellation,
                    "team trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}"
                )
            raise cancellation
        lifecycle_failure = cleanup_failure or tracer_failure
        if lifecycle_failure is not None:
            cause = lifecycle_failure
            if failure is not None:
                cause = failure
                if cleanup_failure is not None:
                    add_exception_note(
                        failure,
                        "team cleanup also failed: "
                        f"{type(cleanup_failure).__name__}: {cleanup_failure}",
                    )
                if tracer_failure is not None:
                    add_exception_note(
                        failure,
                        "team trace also failed: "
                        f"{type(tracer_failure).__name__}: {tracer_failure}",
                    )
            elif cleanup_failure is not None and tracer_failure is not None:
                add_exception_note(
                    cleanup_failure,
                    "team trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise ProgrammaticLifecycleError(
                "team cleanup or trajectory persistence failed"
            ) from cause

    _programmatic._verify_artifact_claim(artifacts)
    if artifacts is not None:
        _programmatic._require_json_object(artifacts / "team.json", "team manifest")
    lead = scheduler.lead_session
    return ProgrammaticResult(
        output=output,
        status=status,
        reason=reason,
        tokens=scheduler.used_tokens,
        artifacts=artifacts,
        error=failure,
        metrics={
            "steps": int(getattr(lead, "step_count", 0)),
            "sessions": len(scheduler.table.entries),
        },
        agent_failures=_programmatic._team_agent_failures(scheduler),
    )
