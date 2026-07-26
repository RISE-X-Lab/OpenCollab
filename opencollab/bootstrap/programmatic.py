"""Programmatic composition root shared by the compact Python API.

This module owns concrete environments, agents, tools, tracing, and lifecycle
evidence.  The public SDK delegates here instead of becoming a second
composition root.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opencollab.adapters.env import (
    DockerEnvironment,
    DockerWorkspaceEnvironment,
    LocalEnvironment,
    WorktreeEnvironment,
)
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.adapters.trace import Tracer
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import EnvironmentPort
from opencollab.bootstrap.agent_runtime import (
    AgentRuntimeLifecycleError,
    AgentRuntimeResult,
    run_environment_hook,
)
from opencollab.bootstrap.agent_runtime import (
    run_agent as _run_agent,
)
from opencollab.bootstrap.runtime_context import build_runtime_context
from opencollab.bootstrap.scheduler_factory import build_scheduler
from opencollab.bootstrap.tool_registry import build_tools_for_role
from opencollab.bootstrap.workflow_runtime import (
    WORKFLOW_AGENT_PROMPT,
    WorkflowDeadlineExceeded,
    WorkflowLifecycleError,
    WorkflowRuntimeResult,
)
from opencollab.bootstrap.workflow_runtime import (
    run_workflow as _run_workflow,
)
from opencollab.domain.agent import Agent

DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are an autonomous software-engineering agent. Complete the user task, "
    "use the available tools when needed, and report the verified result."
)
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 2.0

_ARTIFACT_CLAIM_FILENAME = ".opencollab-run"
_ARTIFACT_CLAIM = b"claimed\n"
_TOOL_PRESETS: dict[str, tuple[str, ...]] = {
    "coding": (
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "git_diff",
        "grep",
    ),
    "read": ("file_read", "grep", "git_diff"),
}


class ProgrammaticLifecycleError(RuntimeError):
    """A run could not reach a proven quiescent and persisted state."""


@dataclass(slots=True)
class ProgrammaticResult:
    """Internal result shared by agent, team, and workflow entry points."""

    output: Any
    status: Literal["completed", "stopped", "failed"]
    reason: str | None
    tokens: int | None
    artifacts: Path | None
    error: BaseException | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    agent_failures: tuple[dict[str, Any], ...] = ()


def resolve_tools(value: str | Sequence[Any] | None) -> tuple[Any, ...]:
    """Resolve a named built-in preset or preserve caller-supplied tools."""
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            names = _TOOL_PRESETS[value]
        except KeyError as exc:
            raise ValueError(
                f"unknown tool preset {value!r}; choose from {sorted(_TOOL_PRESETS)}"
            ) from exc
        return tuple(build_tools_for_role(list(names), interactive=True))
    if not isinstance(value, Sequence):
        raise TypeError("tools must be a preset name or a sequence")
    return tuple(value)


def _trimmed(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\0", "\n", "\r"))
    ):
        raise ValueError(
            f"{name} must be non-empty trimmed text without control characters"
        )
    return value


def attach_container(
    *,
    container_id: str,
    workspace: str,
    command_prefix: Callable[[str], str] | str | None = None,
    timeout_returncode: int = -1,
) -> EnvironmentPort:
    """Attach to a caller-owned container workspace."""
    container_id = _trimmed(container_id, "container_id")
    workspace = _trimmed(workspace, "workspace")
    if not posixpath.isabs(workspace) or posixpath.normpath(workspace) != workspace:
        raise ValueError("workspace must be a normalized absolute container path")
    if isinstance(timeout_returncode, bool) or not isinstance(timeout_returncode, int):
        raise ValueError("timeout_returncode must be an integer")
    return DockerWorkspaceEnvironment(
        container_id=container_id,
        repo_root=workspace,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


def local_environment(workspace: str | os.PathLike[str]) -> EnvironmentPort:
    """Create a host environment rooted at an existing workspace."""
    return LocalEnvironment(os.fspath(workspace))


def worktree_environment(
    source_workspace: str | os.PathLike[str],
) -> EnvironmentPort:
    """Create an uninitialized isolated worktree owned by the caller."""
    return WorktreeEnvironment(os.fspath(source_workspace))


def docker_environment(
    image: str,
    backing_environment: EnvironmentPort | None = None,
) -> EnvironmentPort:
    """Create an uninitialized image-backed Docker environment."""
    return DockerEnvironment(
        image=image,
        backing_environment=backing_environment,
    )


def _claim_artifacts(path: Path | None) -> None:
    if path is None:
        return
    ensure_directory_no_symlinks(path)
    if any(path.iterdir()):
        raise ValueError("artifacts directory must be new or empty")
    try:
        create_regular_bytes_atomic(
            path / _ARTIFACT_CLAIM_FILENAME,
            _ARTIFACT_CLAIM,
            max_bytes=len(_ARTIFACT_CLAIM),
        )
    except FileExistsError as exc:
        raise ValueError("artifacts directory is already claimed") from exc


def _verify_artifact_claim(path: Path | None) -> None:
    if path is None:
        return
    claim_path = path / _ARTIFACT_CLAIM_FILENAME
    try:
        value = read_regular_text(claim_path, max_bytes=len(_ARTIFACT_CLAIM))
    except (OSError, UnicodeError) as exc:
        raise ProgrammaticLifecycleError(
            f"cannot verify run artifact claim: {claim_path}"
        ) from exc
    if value.encode("ascii") != _ARTIFACT_CLAIM:
        raise ProgrammaticLifecycleError(
            f"run artifact claim changed during execution: {claim_path}"
        )


def _require_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_text(path, max_bytes=64 * 1024 * 1024))
        if not isinstance(value, dict):
            raise OSError(f"{description} is not a JSON object")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProgrammaticLifecycleError(f"{description} is incomplete: {path}") from exc
    return value


def _require_agent_evidence(
    result: AgentRuntimeResult,
    artifacts: Path | None,
    transcript_path: Path | None,
    tracer: Tracer | None,
) -> None:
    if not result.cleanup_quiesced or result.persistence_errors:
        raise ProgrammaticLifecycleError(
            "agent cleanup or final transcript persistence failed"
        )
    _verify_artifact_claim(artifacts)
    if artifacts is not None:
        if transcript_path is None:
            raise ProgrammaticLifecycleError("agent transcript path is missing")
        _require_json_object(transcript_path, "agent transcript")
    if tracer is not None:
        try:
            tracer.flush()
        except Exception as exc:
            raise ProgrammaticLifecycleError(
                "agent trajectory persistence failed"
            ) from exc
        if tracer.write_error is not None:
            raise ProgrammaticLifecycleError(
                "agent trajectory persistence failed: " + tracer.write_error
            )


def _agent_status(
    result: AgentRuntimeResult,
) -> tuple[Literal["completed", "stopped", "failed"], str | None]:
    if result.outcome == "failed" or result.phase == "error":
        return "failed", result.terminal_reason or "agent failed"
    if result.outcome == "timed_out":
        return "stopped", "timeout"
    if result.phase == "stopped":
        return "stopped", result.terminal_reason
    return "completed", None


async def run_agent(
    *,
    prompt: str,
    config: Mapping[str, Any],
    workspace: str,
    tools: str | Sequence[Any] | None,
    max_tokens: int,
    max_steps: int,
    timeout: float | None,
    cleanup_timeout: float,
    artifacts: Path | None,
    trace: bool,
    environment: Any | None = None,
    name: str = "agent",
    system_prompt: str = DEFAULT_AGENT_SYSTEM_PROMPT,
    llm: Any | None = None,
) -> ProgrammaticResult:
    """Run one directly configured agent behind the hardened lifecycle."""
    resolved_tools = resolve_tools(tools)
    agent = Agent(
        name=name,
        system_prompt=system_prompt,
        tools=list(resolved_tools),
        model=config["model"],
        provider=config["provider"],
        api_key=config.get("api_key"),
        base_url=config.get("base_url"),
        max_tokens_per_step=config.get("max_output_tokens", 8_192),
        temperature=config.get("temperature", 0.2),
        top_p=config.get("top_p"),
        thinking=config.get("thinking", False),
        thinking_params=dict(config.get("thinking_params") or {}),
    )
    _claim_artifacts(artifacts)
    owned_environment = environment is None
    resolved_environment = environment or LocalEnvironment(workspace)
    transcript_path = None if artifacts is None else artifacts / "agent.json"
    tracer = None
    if artifacts is not None and trace:
        tracer = Tracer(
            run_id=name,
            output_dir=str(artifacts),
            filename="trajectory.jsonl",
        )
    primary_failure: BaseException | None = None
    try:
        try:
            internal = await _run_agent(
                agent=agent,
                environment=resolved_environment,
                prompt=prompt,
                max_tokens=max_tokens,
                max_steps=max_steps,
                timeout_seconds=timeout,
                cleanup_timeout_seconds=cleanup_timeout,
                transcript_path=(
                    None if transcript_path is None else str(transcript_path)
                ),
                tracer=tracer,
                llm=llm,
                llm_timeout_seconds=config.get("llm_timeout", 600.0),
                cleanup_environment=owned_environment,
            )
        except AgentRuntimeLifecycleError as exc:
            raise ProgrammaticLifecycleError(str(exc)) from exc
        _require_agent_evidence(internal, artifacts, transcript_path, tracer)
        status, reason = _agent_status(internal)
        return ProgrammaticResult(
            output=internal.output,
            status=status,
            reason=reason,
            tokens=internal.tokens_spent,
            artifacts=artifacts,
            error=internal.error,
            metrics={
                "steps": internal.step_count,
                "outcome": internal.outcome,
                "phase": internal.phase,
                "terminal_reason": internal.terminal_reason,
                "markup_recovered": internal.markup_recovered,
                "cleanup_quiesced": True,
                "execution_quiesced": True,
            },
        )
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        tracer_failure = _close_tracer(tracer)
        if tracer_failure is not None:
            if primary_failure is not None:
                add_exception_note(
                    primary_failure,
                    "agent trajectory close also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            else:
                raise ProgrammaticLifecycleError(
                    "agent trajectory persistence failed"
                ) from tracer_failure


def _environment_paths(environment: Any, workspace: str) -> tuple[str, str]:
    workdir = getattr(environment, "workspace", None) or workspace
    source_root = getattr(environment, "source_workspace", None) or workspace
    return str(workdir), str(source_root)


def _workflow_metrics(details: WorkflowRuntimeResult | None) -> dict[str, Any]:
    return {
        "steps": 0 if details is None else details.steps,
        "sessions": 0 if details is None else details.sessions,
        "markup_recovered": 0 if details is None else details.markup_recovered,
        "execution_quiesced": details is not None,
    }


async def run_workflow(
    *,
    workflow: Any,
    inputs: Mapping[str, Any],
    config: Mapping[str, Any],
    workspace: str,
    max_tokens: int | None,
    max_concurrency: int,
    timeout: float | None,
    max_steps: int,
    system_prompt: str | None,
    cleanup_timeout: float,
    artifacts: Path | None,
    trace: bool,
    environment: Any | None = None,
) -> ProgrammaticResult:
    """Run one workflow and return its live metrics directly."""
    _claim_artifacts(artifacts)
    owned_environment = environment is None
    resolved_environment = environment or LocalEnvironment(workspace)
    workdir, source_root = _environment_paths(resolved_environment, workspace)
    deadline = (
        None
        if timeout is None
        else asyncio.get_running_loop().time() + timeout
    )
    details: WorkflowRuntimeResult | None = None
    stopped_error: BaseException | None = None
    failed_error: BaseException | None = None
    bootstrap_stopped_environment = False
    try:
        try:
            details = await _run_workflow(
                workflow,
                dict(inputs),
                cfg=dict(config),
                workspace=workdir,
                budget=max_tokens,
                max_concurrency=max_concurrency,
                save_dir=None if artifacts is None else str(artifacts),
                trace=trace,
                env=resolved_environment,
                cleanup_timeout=cleanup_timeout,
                source_root=source_root,
                deadline_monotonic=deadline,
                max_steps=max_steps,
                system_prompt=system_prompt or WORKFLOW_AGENT_PROMPT,
                return_details=True,
            )
        except WorkflowDeadlineExceeded as exc:
            bootstrap_stopped_environment = True
            stopped_error = exc
        except WorkflowLifecycleError as exc:
            raise ProgrammaticLifecycleError(str(exc)) from exc
        except asyncio.CancelledError:
            bootstrap_stopped_environment = True
            raise
        except Exception as exc:
            runtime_result = getattr(exc, "runtime_result", None)
            if (
                runtime_result is None
                or not runtime_result.evidence_complete
            ):
                raise ProgrammaticLifecycleError(
                    "workflow runtime failed without finalized execution evidence"
                ) from exc
            failed_error = exc
    finally:
        if owned_environment and not bootstrap_stopped_environment:
            cleaned = await await_owned_operation(
                run_environment_hook(
                    resolved_environment,
                    "cleanup",
                    timeout=cleanup_timeout,
                ),
                propagate_cancellation=True,
            )
            if not cleaned:
                raise ProgrammaticLifecycleError(
                    "workflow-owned environment cleanup failed"
                )

    _verify_artifact_claim(artifacts)
    if stopped_error is not None:
        details = stopped_error.result
        metrics = _workflow_metrics(details)
        metrics["execution_quiesced"] = True
        return ProgrammaticResult(
            output=None,
            status="stopped",
            reason="timeout",
            tokens=0 if details is None else details.tokens,
            artifacts=artifacts,
            error=stopped_error,
            metrics=metrics,
            agent_failures=() if details is None else details.agent_failures,
        )
    if failed_error is not None:
        details = getattr(failed_error, "runtime_result", None)
        return ProgrammaticResult(
            output=None,
            status="failed",
            reason=str(failed_error) or type(failed_error).__name__,
            tokens=None if details is None else details.tokens,
            artifacts=artifacts,
            error=failed_error,
            metrics=_workflow_metrics(details),
            agent_failures=() if details is None else details.agent_failures,
        )
    if details is None:
        raise ProgrammaticLifecycleError("workflow completed without a result")
    output = details.output
    budget_stopped = details.stop_reason == "budget_exceeded"
    return ProgrammaticResult(
        output=output,
        status="stopped" if budget_stopped else "completed",
        reason="budget_exceeded" if budget_stopped else None,
        tokens=details.tokens,
        artifacts=artifacts,
        metrics=_workflow_metrics(details),
        agent_failures=details.agent_failures,
    )


def _close_tracer(tracer: Tracer | None) -> BaseException | None:
    if tracer is None:
        return None
    try:
        tracer.close()
    except BaseException as exc:
        return exc
    if tracer.write_error is not None:
        return OSError("trajectory persistence failed: " + tracer.write_error)
    return None


async def run_team(
    *,
    prompt: str,
    config: Mapping[str, Any],
    workspace: str,
    team_config_path: str | os.PathLike[str] | None,
    max_tokens: int,
    timeout: float | None,
    artifacts: Path | None,
    trace: bool,
    use_worktrees: bool,
) -> ProgrammaticResult:
    """Run the scheduler regime once, including bounded team cleanup."""
    _claim_artifacts(artifacts)
    run_config = dict(config)
    run_config["budget"] = max_tokens
    context = build_runtime_context(workspace, run_config, trace=False)
    if artifacts is not None and trace:
        context.tracer = Tracer(
            run_id="team",
            output_dir=str(artifacts),
            filename="trajectory.jsonl",
        )
    try:
        scheduler = build_scheduler(
            context,
            use_worktrees=use_worktrees,
            interactive=False,
            auto_save=artifacts is not None,
            team_config_path=team_config_path,
            save_dir=artifacts,
        )
    except BaseException as exc:
        tracer_failure = _close_tracer(context.tracer)
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
            await scheduler.cleanup(
                cleanup_timeout=DEFAULT_CLEANUP_TIMEOUT_SECONDS
            )
        except BaseException as exc:
            cleanup_failure = exc
        tracer_failure = _close_tracer(context.tracer)
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
            raise ProgrammaticLifecycleError(
                "team cleanup or trajectory persistence failed"
            ) from lifecycle_failure

    _verify_artifact_claim(artifacts)
    if artifacts is not None:
        _require_json_object(artifacts / "team.json", "team manifest")
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
    )


__all__ = [
    "DEFAULT_AGENT_SYSTEM_PROMPT",
    "ProgrammaticLifecycleError",
    "ProgrammaticResult",
    "attach_container",
    "docker_environment",
    "local_environment",
    "resolve_tools",
    "run_agent",
    "run_team",
    "run_workflow",
    "worktree_environment",
]
