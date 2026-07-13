"""Stable SDK wrapper around the hardened OpenCollab workflow lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.adapters.trace import Tracer
from opencollab.application.async_timeout import force_task_terminal
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap.container import build_workspace_safety_policy
from opencollab.bootstrap.session_factory import WORKFLOW_MANIFEST_FILENAME, build_session
from opencollab.bootstrap.workflow_runtime import run_workflow as _run_hardened_workflow
from opencollab.domain.agent import Agent

from .environment import ExecutionEnvironment
from .errors import (
    AgentRunLifecycleError,
    AgentRunTimeoutError,
    InvalidSDKRequestError,
    WorkflowManifestError,
    WorkflowRunLifecycleError,
    WorkflowRunTimeoutError,
)
from .files import open_directory_no_symlinks
from .models import AgentRunRequest, AgentRunResult, WorkflowRunRequest, WorkflowRunResult


class _RaisedInnerTimeout:
    def __init__(self, error: TimeoutError) -> None:
        self.error = error


class OpenCollabRuntime:
    """Execute workflows through the existing hardened lifecycle boundary."""

    async def run_workflow(self, request: WorkflowRunRequest) -> WorkflowRunResult:
        """Run a workflow and return after owned runtime activity is quiescent.

        A non-null artifact directory is reserved for exactly one invocation.
        Retries must use a fresh attempt directory so evidence from separate
        executions cannot be combined.
        """
        if not isinstance(request, WorkflowRunRequest):
            raise TypeError("request must be a WorkflowRunRequest")

        started_at = time.monotonic()
        environment_workdir = request.environment_workdir or request.workspace
        if environment_workdir is None and request.environment is not None:
            environment_workdir = getattr(request.environment, "workspace", None)
        source_root = request.source_root
        if source_root is None and request.workspace is not None:
            source_root = request.workspace
        if source_root is None and request.environment is not None:
            source_root = getattr(request.environment, "source_workspace", None)
        environment_owned = request.environment is None
        environment: ExecutionEnvironment = request.environment or LocalEnvironment(
            environment_workdir or source_root or "."
        )
        deadline = None if request.budget.timeout_seconds is None else started_at + request.budget.timeout_seconds
        artifact_dir = request.artifact_dir
        artifact_claim: bytes | None = None
        if artifact_dir is not None:
            artifact_claim = _claim_artifact_dir(artifact_dir)

        operation = _run_hardened_workflow(
            request.workflow,
            deepcopy(dict(request.inputs)),
            cfg=request.config.as_runtime_dict(),
            workspace=environment_workdir,
            budget=request.budget.max_tokens,
            max_concurrency=request.budget.max_concurrency,
            save_dir=None if artifact_dir is None else str(artifact_dir),
            trace=request.trace,
            env=environment,
            cleanup_timeout=request.budget.cleanup_timeout_seconds,
            source_root=source_root,
            deadline_monotonic=deadline,
            deadline_margin_seconds=request.budget.deadline_margin_seconds,
        )
        owner = asyncio.create_task(_preserve_inner_timeout(operation))
        try:
            if deadline is None:
                timed_output = await asyncio.shield(owner)
            else:
                remaining = max(0.0, deadline - time.monotonic())
                _done, pending = await asyncio.wait({owner}, timeout=remaining)
                if pending:
                    abort_succeeded = await _abort_environment(
                        environment,
                        timeout=request.budget.cleanup_timeout_seconds,
                    )
                    termination = await force_task_terminal(
                        owner,
                        timeout=request.budget.cleanup_timeout_seconds,
                    )
                    if not abort_succeeded or not termination.terminal:
                        raise WorkflowRunLifecycleError("timed-out workflow abort or task termination failed")
                    raise WorkflowRunTimeoutError(f"workflow exceeded {request.budget.timeout_seconds:g} seconds")
                timed_output = owner.result()
        except asyncio.CancelledError:
            await _abort_environment(
                environment,
                timeout=request.budget.cleanup_timeout_seconds,
            )
            if not owner.done():
                await force_task_terminal(
                    owner,
                    timeout=request.budget.cleanup_timeout_seconds,
                )
            if environment_owned:
                await _run_environment_hook(
                    environment,
                    "cleanup",
                    timeout=request.budget.cleanup_timeout_seconds,
                )
            raise
        except Exception:
            if environment_owned:
                await _run_environment_hook(
                    environment,
                    "cleanup",
                    timeout=request.budget.cleanup_timeout_seconds,
                )
            raise
        if environment_owned:
            cleanup_succeeded = await _run_environment_hook(
                environment,
                "cleanup",
                timeout=request.budget.cleanup_timeout_seconds,
            )
            if not cleanup_succeeded:
                raise WorkflowRunLifecycleError("workflow-owned environment cleanup failed")
        if isinstance(timed_output, _RaisedInnerTimeout):
            raise timed_output.error
        output = timed_output

        manifest_path = None
        tokens_spent = None
        session_count = None
        if artifact_dir is not None:
            _verify_artifact_claim(artifact_dir, artifact_claim)
            manifest_path = artifact_dir / WORKFLOW_MANIFEST_FILENAME
            manifest = _read_manifest(manifest_path)
            tokens_spent = _non_negative_manifest_integer(manifest, "tokens_spent", manifest_path)
            session_count = _non_negative_manifest_integer(manifest, "sessions", manifest_path)

        return WorkflowRunResult(
            output=output,
            workflow_name=_workflow_name(request.workflow),
            tokens_spent=tokens_spent,
            session_count=session_count,
            artifact_dir=artifact_dir,
            manifest_path=manifest_path,
        )

    async def run_agent(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one agent while owning its timeout, persistence, and cleanup."""
        if not isinstance(request, AgentRunRequest):
            raise TypeError("request must be an AgentRunRequest")

        started_at = time.monotonic()
        deadline = None if request.budget.timeout_seconds is None else started_at + request.budget.timeout_seconds
        environment_workdir = request.environment_workdir or request.workspace
        if environment_workdir is None and request.environment is not None:
            environment_workdir = getattr(request.environment, "workspace", None)
        source_root = request.source_root
        if source_root is None and request.workspace is not None:
            source_root = request.workspace
        if source_root is None and request.environment is not None:
            source_root = getattr(request.environment, "source_workspace", None)
        environment_owned = request.environment is None
        environment: ExecutionEnvironment = request.environment or LocalEnvironment(
            environment_workdir or source_root or "."
        )

        artifact_dir = request.artifact_dir
        artifact_claim: bytes | None = None
        if artifact_dir is not None:
            artifact_claim = _claim_artifact_dir(artifact_dir)
        transcript_path = None if artifact_dir is None else artifact_dir / "agent.json"
        tracer = None
        trace_path = None
        if artifact_dir is not None and request.trace:
            tracer = Tracer(
                run_id=request.name,
                output_dir=str(artifact_dir),
                filename="trajectory.jsonl",
            )
            trace_path = Path(tracer.path)

        owner: asyncio.Task[str] | None = None
        session: Any | None = None
        lifecycle_finalized = False
        try:
            config = request.config
            agent = Agent(
                name=request.name,
                system_prompt=request.system_prompt,
                tools=list(request.tools),
                model=config.model,
                provider=config.provider,
                api_key=config.api_key,
                base_url=config.base_url,
                max_tokens_per_step=config.max_output_tokens,
                temperature=config.temperature,
                top_p=config.top_p,
                thinking=config.thinking,
                thinking_params=dict(config.thinking_params),
            )
            session = build_session(
                agent=agent,
                env=environment,
                tracer=tracer,
                max_budget_tokens=request.budget.max_tokens,
                max_steps=request.budget.max_steps,
                auto_save_path=None if transcript_path is None else str(transcript_path),
                safety_policy=build_workspace_safety_policy(environment),
                llm=request.llm,
                llm_timeout=config.llm_timeout_seconds,
            )
            owner = asyncio.create_task(_run_agent_session(session, request.prompt))
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, pending = await asyncio.wait(
                {owner},
                timeout=remaining,
            )
            if pending:
                abort_succeeded = await _abort_environment(
                    environment,
                    timeout=request.budget.cleanup_timeout_seconds,
                )
                termination = await force_task_terminal(
                    owner,
                    timeout=request.budget.cleanup_timeout_seconds,
                )
                cleanup_quiesced = await _finalize_agent_session(
                    session,
                    environment,
                    timeout=request.budget.cleanup_timeout_seconds,
                    cleanup_environment=environment_owned,
                )
                lifecycle_finalized = True
                if not abort_succeeded or not termination.terminal or not cleanup_quiesced:
                    raise AgentRunLifecycleError("timed-out agent did not reach a quiescent terminal state")
                _require_agent_evidence(
                    session,
                    artifact_dir=artifact_dir,
                    artifact_claim=artifact_claim,
                    transcript_path=transcript_path,
                    tracer=tracer,
                    cleanup_quiesced=cleanup_quiesced,
                )
                if request.failure_mode == "return":
                    return _agent_result(
                        session,
                        outcome="timed_out",
                        output=None,
                        error=AgentRunTimeoutError(f"agent exceeded {request.budget.timeout_seconds:g} seconds"),
                        artifact_dir=artifact_dir,
                        transcript_path=transcript_path,
                        trace_path=trace_path,
                    )
                raise AgentRunTimeoutError(f"agent exceeded {request.budget.timeout_seconds:g} seconds")

            try:
                output = owner.result()
            except Exception as exc:
                cleanup_quiesced = await _finalize_agent_session(
                    session,
                    environment,
                    timeout=request.budget.cleanup_timeout_seconds,
                    cleanup_environment=environment_owned,
                )
                lifecycle_finalized = True
                _require_agent_evidence(
                    session,
                    artifact_dir=artifact_dir,
                    artifact_claim=artifact_claim,
                    transcript_path=transcript_path,
                    tracer=tracer,
                    cleanup_quiesced=cleanup_quiesced,
                )
                if request.failure_mode == "return":
                    return _agent_result(
                        session,
                        outcome="failed",
                        output=None,
                        error=exc,
                        artifact_dir=artifact_dir,
                        transcript_path=transcript_path,
                        trace_path=trace_path,
                    )
                raise
            cleanup_quiesced = await _finalize_agent_session(
                session,
                environment,
                timeout=request.budget.cleanup_timeout_seconds,
                cleanup_environment=environment_owned,
            )
            lifecycle_finalized = True
            _require_agent_evidence(
                session,
                artifact_dir=artifact_dir,
                artifact_claim=artifact_claim,
                transcript_path=transcript_path,
                tracer=tracer,
                cleanup_quiesced=cleanup_quiesced,
            )
            return _agent_result(
                session,
                outcome="completed",
                output=output,
                error=None,
                artifact_dir=artifact_dir,
                transcript_path=transcript_path,
                trace_path=trace_path,
            )
        except asyncio.CancelledError:
            await _abort_environment(
                environment,
                timeout=request.budget.cleanup_timeout_seconds,
            )
            if owner is not None and not owner.done():
                await force_task_terminal(
                    owner,
                    timeout=request.budget.cleanup_timeout_seconds,
                )
            if session is not None:
                await _finalize_agent_session(
                    session,
                    environment,
                    timeout=request.budget.cleanup_timeout_seconds,
                    cleanup_environment=environment_owned,
                )
            elif environment_owned:
                await _run_environment_hook(
                    environment,
                    "cleanup",
                    timeout=request.budget.cleanup_timeout_seconds,
                )
            lifecycle_finalized = True
            raise
        except Exception:
            if not lifecycle_finalized:
                if owner is not None and not owner.done():
                    await _abort_environment(
                        environment,
                        timeout=request.budget.cleanup_timeout_seconds,
                    )
                    await force_task_terminal(
                        owner,
                        timeout=request.budget.cleanup_timeout_seconds,
                    )
                if session is not None:
                    await _finalize_agent_session(
                        session,
                        environment,
                        timeout=request.budget.cleanup_timeout_seconds,
                        cleanup_environment=environment_owned,
                    )
                elif environment_owned:
                    await _run_environment_hook(
                        environment,
                        "cleanup",
                        timeout=request.budget.cleanup_timeout_seconds,
                    )
                lifecycle_finalized = True
            raise
        finally:
            if tracer is not None:
                tracer.close()


async def _run_agent_session(session: Any, prompt: str) -> str:
    await session.add_user_message(prompt)
    return await session.run_loop()


def _require_agent_evidence(
    session: Any,
    *,
    artifact_dir: Path | None,
    artifact_claim: bytes | None,
    transcript_path: Path | None,
    tracer: Tracer | None,
    cleanup_quiesced: bool,
) -> None:
    if not cleanup_quiesced or session.persistence_errors:
        raise AgentRunLifecycleError("agent cleanup or final transcript persistence failed")
    if artifact_dir is not None:
        try:
            _verify_artifact_claim(artifact_dir, artifact_claim)
            if transcript_path is None:
                raise OSError("agent transcript path is missing")
            transcript = json.loads(read_regular_text(transcript_path, max_bytes=64 * 1024 * 1024))
            if not isinstance(transcript, dict):
                raise OSError("agent transcript is not a JSON object")
        except (OSError, UnicodeError, json.JSONDecodeError, WorkflowManifestError) as exc:
            raise AgentRunLifecycleError("agent artifact evidence is incomplete") from exc
    if tracer is not None:
        try:
            tracer.flush()
        except Exception as exc:
            raise AgentRunLifecycleError("agent trajectory persistence failed") from exc
        if tracer.write_error is not None:
            raise AgentRunLifecycleError("agent trajectory persistence failed: " + tracer.write_error)


def _agent_result(
    session: Any,
    *,
    outcome: Literal["completed", "timed_out", "failed"],
    output: str | None,
    error: Exception | None,
    artifact_dir: Path | None,
    transcript_path: Path | None,
    trace_path: Path | None,
) -> AgentRunResult:
    return AgentRunResult(
        output=output,
        outcome=outcome,
        phase=session.phase.value,
        terminal_reason=session.state.terminal_reason,
        error_type=None if error is None else type(error).__name__,
        error_message=None if error is None else str(error),
        tokens_spent=session.used_tokens,
        step_count=session.step_count,
        artifact_dir=artifact_dir,
        transcript_path=transcript_path,
        trace_path=trace_path,
        cleanup_quiesced=True,
    )


async def _run_environment_hook(
    environment: ExecutionEnvironment,
    name: str,
    *,
    timeout: float,
) -> bool:
    hook = getattr(environment, name, None)
    if not callable(hook):
        return False
    try:
        outcome = hook()
    except Exception:
        return False
    if not inspect.isawaitable(outcome):
        return True

    try:
        owner = asyncio.ensure_future(outcome)
    except Exception:
        close = getattr(outcome, "close", None)
        if callable(close):
            close()
        return False
    done, pending = await asyncio.wait({owner}, timeout=timeout)
    if pending:
        await force_task_terminal(owner, timeout=timeout)
        return False
    try:
        owner.result()
    except BaseException:
        return False
    return True


async def _abort_environment(
    environment: ExecutionEnvironment,
    *,
    timeout: float,
) -> bool:
    revoked = True
    try:
        environment.revoke()
    except Exception:
        revoked = False
    aborted = await _run_environment_hook(environment, "abort", timeout=timeout)
    return revoked and aborted


async def _wait_for_session_tasks(session: Any, *, timeout: float) -> bool:
    """Wait through one stable empty turn for session-owned async work."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    saw_empty = False
    while True:
        pending = {
            task for task in session.pending_cleanup_tasks if not task.done() and task is not asyncio.current_task()
        }
        if not pending:
            if saw_empty:
                return True
            saw_empty = True
            await asyncio.sleep(0)
            continue
        saw_empty = False
        remaining = deadline - loop.time()
        if remaining <= 0:
            for task in pending:
                await force_task_terminal(task, timeout=timeout)
            return False
        _done, still_pending = await asyncio.wait(pending, timeout=remaining)
        if still_pending:
            for task in still_pending:
                await force_task_terminal(task, timeout=timeout)
            return False


async def _finalize_agent_session(
    session: Any,
    environment: ExecutionEnvironment,
    *,
    timeout: float,
    cleanup_environment: bool,
) -> bool:
    quiesced_before_save = await _wait_for_session_tasks(session, timeout=timeout)
    save_enqueued = False
    if quiesced_before_save:
        try:
            save_owner = session.enqueue_auto_save()
            save_enqueued = session.auto_save_path is None or save_owner is not None
        except Exception:
            pass
    quiesced_after_save = await _wait_for_session_tasks(session, timeout=timeout)
    cleanup_succeeded = True
    if cleanup_environment:
        cleanup_succeeded = await _run_environment_hook(
            environment,
            "cleanup",
            timeout=timeout,
        )
    return quiesced_before_save and save_enqueued and quiesced_after_save and cleanup_succeeded


_SDK_ARTIFACT_CLAIM_FILENAME = ".opencollab-sdk-run"


def _claim_artifact_dir(artifact_dir: Path) -> bytes:
    ensure_directory_no_symlinks(artifact_dir)
    claim = secrets.token_hex(32).encode("ascii")
    directory_fd = open_directory_no_symlinks(artifact_dir)
    try:
        if os.listdir(directory_fd):
            raise InvalidSDKRequestError("artifact_dir already contains run evidence or is claimed")
        opened = os.fstat(directory_fd)
        create_regular_bytes_atomic(
            artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME,
            claim,
            max_bytes=len(claim),
            expected_parent_identity=(opened.st_dev, opened.st_ino),
        )
    except FileExistsError as exc:
        raise InvalidSDKRequestError("artifact_dir is already claimed by an SDK run") from exc
    finally:
        os.close(directory_fd)
    return claim


def _verify_artifact_claim(artifact_dir: Path, expected: bytes | None) -> None:
    if expected is None:
        raise WorkflowManifestError("SDK artifact claim is missing")
    claim_path = artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME
    try:
        actual = read_regular_text(claim_path, max_bytes=len(expected)).encode("ascii")
    except (OSError, UnicodeError) as exc:
        raise WorkflowManifestError(f"cannot verify SDK artifact claim: {claim_path}") from exc
    if not secrets.compare_digest(actual, expected):
        raise WorkflowManifestError(f"SDK artifact claim changed during workflow execution: {claim_path}")


async def _preserve_inner_timeout(operation: Any) -> Any:
    try:
        return await operation
    except asyncio.TimeoutError as exc:
        return _RaisedInnerTimeout(exc)


def _workflow_name(workflow: Any) -> str:
    if isinstance(workflow, WorkflowSpec):
        return workflow.name
    spec = getattr(workflow, "__workflow_spec__", None)
    if isinstance(spec, WorkflowSpec):
        return spec.name
    return getattr(workflow, "__name__", "workflow")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_regular_text(path, max_bytes=4 * 1024 * 1024))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowManifestError(f"cannot read hardened workflow manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowManifestError(f"hardened workflow manifest is not an object: {path}")
    return payload


def _non_negative_manifest_integer(manifest: dict[str, Any], key: str, path: Path) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowManifestError(f"hardened workflow manifest has invalid {key}: {path}")
    return value


__all__ = ["OpenCollabRuntime"]
