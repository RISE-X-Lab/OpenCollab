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
    Environment,
    LocalEnvironment,
    WorktreeEnvironment,
)
from opencollab.adapters.repo_map import (
    build_repo_map_via_env as _build_repo_map_via_env,
)
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.adapters.storage import SessionStore
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
from opencollab.bootstrap.config import resolve_thinking_params
from opencollab.bootstrap.scheduler_factory import build_scheduler  # noqa: F401
from opencollab.bootstrap.session_factory import SESSION_MAX_STEPS
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
DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS = 10.0

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
        # No preset contains ``ask_user``; what the old ``interactive=True``
        # bought here was the unsandboxed shell, so that is what is asked for.
        return tuple(build_tools_for_role(list(names), allow_unisolated_shell=True))
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
    if (
        workspace.startswith("//")
        or not posixpath.isabs(workspace)
        or posixpath.normpath(workspace) != workspace
        or workspace == "/"
    ):
        raise ValueError("workspace must be a normalized absolute non-root container path")
    if isinstance(timeout_returncode, bool) or not isinstance(timeout_returncode, int):
        raise ValueError("timeout_returncode must be an integer")
    return DockerWorkspaceEnvironment(
        container_id=container_id,
        repo_root=workspace,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


async def build_repo_map_via_env(env: Any, **budgets: int) -> str:
    """A bounded listing of the paths an environment's workspace holds.

    Public because the workspace an agent reads is not always on this host. A
    caller that runs agents inside a container cannot walk the repository with
    ``os.walk`` -- the directory it would walk is the one the run was launched
    from, not the one the agent sees -- so it asks the environment instead.

    Returns ``""`` when the listing cannot be taken, so a caller can append the
    result unconditionally.
    """
    return await _build_repo_map_via_env(env, **budgets)


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


def _require_json_workflow_inputs(inputs: Mapping[str, Any]) -> None:
    """Reject inputs that the workflow artifact manifest could not persist."""
    try:
        json.dumps(inputs)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(
            "workflow inputs must be JSON-serializable when artifacts are enabled"
        ) from exc


def _require_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_text(path, max_bytes=64 * 1024 * 1024))
        if not isinstance(value, dict):
            raise OSError(f"{description} is not a JSON object")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProgrammaticLifecycleError(f"{description} is incomplete: {path}") from exc
    return value


def _settle_programmatic_workflow_manifest(
    artifacts: Path | None,
    *,
    cleanup_succeeded: bool,
) -> BaseException | None:
    """Promote or downgrade the manifest after the outer environment owner."""
    if artifacts is None:
        return None
    path = artifacts / "workflow.json"
    try:
        manifest = _require_json_object(path, "workflow manifest")
        if cleanup_succeeded:
            manifest["evidence_complete"] = True
        else:
            manifest["status"] = "failed"
            manifest["reason"] = "environment_cleanup_failed"
            manifest["failure_type"] = "ProgrammaticLifecycleError"
            manifest["evidence_complete"] = False
        SessionStore().save_manifest(path, manifest)
    except BaseException as exc:
        return exc
    return None


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


def _quiescence_metrics(
    *,
    session_quiesced: bool,
    environment_owned: bool,
    environment_cleanup_quiesced: bool | None,
    environment_quiesced: bool | None,
) -> dict[str, bool | None]:
    if not session_quiesced or environment_quiesced is False:
        aggregate: bool | None = False
    elif environment_quiesced is True:
        aggregate = True
    else:
        aggregate = None
    return {
        "session_quiesced": session_quiesced,
        "environment_owned": environment_owned,
        "environment_cleanup_quiesced": environment_cleanup_quiesced,
        "environment_quiesced": environment_quiesced,
        "cleanup_quiesced": aggregate,
        "execution_quiesced": aggregate,
    }


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
        wire_protocol=config.get("wire_protocol", "chat_completions"),
        api_key=config.get("api_key"),
        base_url=config.get("base_url"),
        context_window=config.get("context_window"),
        max_tokens_per_step=config.get("max_output_tokens", 8_192),
        temperature=config.get("temperature", 0.2),
        top_p=config.get("top_p"),
        thinking=config.get("thinking", False),
        thinking_params=resolve_thinking_params(config.get("thinking_params")),
        reasoning_effort=config.get("reasoning_effort"),
        llm_connect_timeout=config.get("llm_connect_timeout", 30.0),
        llm_first_event_timeout=config.get("llm_first_event_timeout", 180.0),
        llm_stream_idle_timeout=config.get("llm_stream_idle_timeout", 180.0),
        llm_stream_chat=bool(config.get("llm_stream_chat", False)),
        llm_max_retries=config.get("llm_max_retries", 3),
        provider_error_time_budget=config.get("provider_error_time_budget", 0.0),
    )
    _claim_artifacts(artifacts)
    owned_environment = environment is None
    resolved_environment = (
        environment if environment is not None else LocalEnvironment(workspace)
    )
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
        quiescence = _quiescence_metrics(
            session_quiesced=internal.cleanup_quiesced,
            environment_owned=owned_environment,
            environment_cleanup_quiesced=internal.environment_cleanup_quiesced,
            environment_quiesced=internal.environment_quiesced,
        )
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
                **quiescence,
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


def _workflow_metrics(
    details: WorkflowRuntimeResult | None,
    *,
    environment_owned: bool,
    environment_cleanup_quiesced: bool | None,
    environment_quiesced: bool | None,
) -> dict[str, Any]:
    metrics = {
        "steps": 0 if details is None else details.steps,
        "sessions": 0 if details is None else details.sessions,
        "markup_recovered": 0 if details is None else details.markup_recovered,
    }
    metrics.update(
        _quiescence_metrics(
            session_quiesced=details is not None,
            environment_owned=environment_owned,
            environment_cleanup_quiesced=environment_cleanup_quiesced,
            environment_quiesced=environment_quiesced,
        )
    )
    return metrics


async def run_workflow(
    *,
    workflow: Any,
    inputs: Mapping[str, Any],
    config: Mapping[str, Any],
    workspace: str,
    max_tokens: int | None,
    max_concurrency: int,
    task_concurrency: int | None = None,
    timeout: float | None,
    max_steps: int,
    system_prompt: str | None,
    cleanup_timeout: float,
    artifacts: Path | None,
    trace: bool,
    environment: Any | None = None,
) -> ProgrammaticResult:
    """Run one workflow and return its live metrics directly."""
    workflow_inputs = dict(inputs)
    if artifacts is not None:
        _require_json_workflow_inputs(workflow_inputs)
    _claim_artifacts(artifacts)
    owned_environment = environment is None
    resolved_environment = (
        environment if environment is not None else LocalEnvironment(workspace)
    )
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
    environment_cleanup_quiesced: bool | None = None
    environment_quiesced: bool | None = None
    try:
        try:
            details = await _run_workflow(
                workflow,
                workflow_inputs,
                cfg=dict(config),
                workspace=workdir,
                budget=max_tokens,
                max_concurrency=max_concurrency,
                task_concurrency=task_concurrency,
                save_dir=None if artifacts is None else str(artifacts),
                trace=trace,
                env=resolved_environment,
                cleanup_timeout=cleanup_timeout,
                source_root=source_root,
                deadline_monotonic=deadline,
                max_steps=max_steps,
                system_prompt=system_prompt or WORKFLOW_AGENT_PROMPT,
                return_details=True,
                cleanup_environment=owned_environment,
                defer_manifest_completion=(
                    owned_environment and artifacts is not None
                ),
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
                manifest_failure = _settle_programmatic_workflow_manifest(
                    artifacts,
                    cleanup_succeeded=False,
                )
                failure = ProgrammaticLifecycleError(
                    "workflow-owned environment cleanup failed"
                )
                if manifest_failure is not None:
                    add_exception_note(
                        failure,
                        "workflow manifest downgrade also failed: "
                        f"{type(manifest_failure).__name__}: {manifest_failure}",
                    )
                    raise failure from manifest_failure
                raise failure
            environment_cleanup_quiesced = True
            environment_quiesced = True

    _verify_artifact_claim(artifacts)
    if owned_environment and artifacts is not None:
        manifest_failure = _settle_programmatic_workflow_manifest(
            artifacts,
            cleanup_succeeded=True,
        )
        if manifest_failure is not None:
            raise ProgrammaticLifecycleError(
                "workflow manifest finalization failed"
            ) from manifest_failure
    if stopped_error is not None:
        details = stopped_error.result
        metrics = _workflow_metrics(
            details,
            environment_owned=owned_environment,
            environment_cleanup_quiesced=(
                True if owned_environment else environment_cleanup_quiesced
            ),
            environment_quiesced=(
                True if owned_environment else environment_quiesced
            ),
        )
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
            metrics=_workflow_metrics(
                details,
                environment_owned=owned_environment,
                environment_cleanup_quiesced=environment_cleanup_quiesced,
                environment_quiesced=environment_quiesced,
            ),
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
        metrics=_workflow_metrics(
            details,
            environment_owned=owned_environment,
            environment_cleanup_quiesced=environment_cleanup_quiesced,
            environment_quiesced=environment_quiesced,
        ),
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


def _team_agent_failures(scheduler: Any) -> tuple[dict[str, Any], ...]:
    """Return bounded, message-free summaries for terminal child agents."""
    failures: list[dict[str, Any]] = []
    entries = getattr(getattr(scheduler, "table", None), "entries", {})
    for aid, scb in sorted(entries.items()):
        if aid == 0:
            continue
        state = getattr(scb, "state", None)
        phase = getattr(getattr(state, "phase", None), "value", None)
        if phase not in {"error", "stopped"}:
            continue
        exception_type = "AgentFailure" if phase == "error" else "AgentStopped"
        reason = getattr(state, "terminal_reason", None)
        if phase == "error" and isinstance(reason, str) and ":" in reason:
            candidate = reason.split(":", 1)[0].strip()
            if (
                candidate
                and len(candidate) <= 128
                and all(char.isalnum() or char in "._-" for char in candidate)
            ):
                exception_type = candidate
        label = str(getattr(getattr(scb, "agent", None), "name", "agent"))[:240]
        failures.append(
            {
                "label": label,
                "exception_type": exception_type,
                "status_code": None,
                "provider_error_type": None,
            }
        )
    return tuple(failures)


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
    max_steps: int = SESSION_MAX_STEPS,
    serialize_turns: bool = False,
    environment: Environment | None = None,
    record_delivery_tree: bool = False,
) -> ProgrammaticResult:
    """Run the scheduler regime once, including bounded team cleanup.

    A forwarder kept so ``programmatic`` stays the one import surface for the
    three regimes; the implementation lives in ``programmatic_team``, whose
    docstring documents ``prebuild_team``, ``allow_unisolated_shell``,
    ``max_steps``, ``serialize_turns``, ``environment`` and
    ``record_delivery_tree``.
    """
    from opencollab.bootstrap.programmatic_team import run_team as _run_team

    return await _run_team(
        prompt=prompt,
        config=config,
        workspace=workspace,
        team_config_path=team_config_path,
        max_tokens=max_tokens,
        timeout=timeout,
        cleanup_timeout=cleanup_timeout,
        artifacts=artifacts,
        trace=trace,
        use_worktrees=use_worktrees,
        prebuild_team=prebuild_team,
        allow_unisolated_shell=allow_unisolated_shell,
        max_steps=max_steps,
        serialize_turns=serialize_turns,
        environment=environment,
        record_delivery_tree=record_delivery_tree,
    )


__all__ = [
    "DEFAULT_AGENT_SYSTEM_PROMPT",
    "DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS",
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
