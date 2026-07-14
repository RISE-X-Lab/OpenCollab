"""Thin public wrapper around OpenCollab bootstrap runtimes."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.adapters.trace import Tracer
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap.agent_runtime import (
    AgentRuntimeLifecycleError,
    AgentRuntimeResult,
    run_environment_hook,
)
from opencollab.bootstrap.agent_runtime import (
    run_agent as _run_bootstrap_agent,
)
from opencollab.bootstrap.session_factory import WORKFLOW_MANIFEST_FILENAME
from opencollab.bootstrap.workflow_runtime import (
    WorkflowDeadlineExceeded,
    WorkflowLifecycleError,
)
from opencollab.bootstrap.workflow_runtime import (
    run_workflow as _run_hardened_workflow,
)
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
from .models import AgentRunRequest, AgentRunResult, WorkflowRunRequest, WorkflowRunResult

_SDK_ARTIFACT_CLAIM_FILENAME = ".opencollab-sdk-run"
_SDK_ARTIFACT_CLAIM = b"claimed\n"


class OpenCollabRuntime:
    """Translate stable SDK values to the shared bootstrap runtimes."""

    async def run_workflow(self, request: WorkflowRunRequest) -> WorkflowRunResult:
        if not isinstance(request, WorkflowRunRequest):
            raise TypeError("request must be a WorkflowRunRequest")
        workdir, source_root = _workspace_paths(request)
        owned = request.environment is None
        environment: ExecutionEnvironment = request.environment or LocalEnvironment(workdir or source_root or ".")
        if request.artifact_dir is not None:
            _claim_artifact_dir(request.artifact_dir)
        loop = asyncio.get_running_loop()
        deadline = (
            None
            if request.budget.timeout_seconds is None
            else loop.time() + request.budget.timeout_seconds
        )
        bootstrap_stopped_environment = False
        try:
            try:
                output = await _run_hardened_workflow(
                    request.workflow,
                    deepcopy(dict(request.inputs)),
                    cfg=request.config.as_runtime_dict(),
                    workspace=workdir,
                    budget=request.budget.max_tokens,
                    max_concurrency=request.budget.max_concurrency,
                    save_dir=None if request.artifact_dir is None else str(request.artifact_dir),
                    trace=request.trace,
                    env=environment,
                    cleanup_timeout=request.budget.cleanup_timeout_seconds,
                    source_root=source_root,
                    deadline_monotonic=deadline,
                    deadline_margin_seconds=request.budget.deadline_margin_seconds,
                )
            except WorkflowDeadlineExceeded as exc:
                bootstrap_stopped_environment = True
                raise WorkflowRunTimeoutError(
                    f"workflow exceeded {request.budget.timeout_seconds:g} seconds"
                ) from exc
            except WorkflowLifecycleError as exc:
                raise WorkflowRunLifecycleError(str(exc)) from exc
            except asyncio.CancelledError:
                bootstrap_stopped_environment = True
                raise
        finally:
            if owned and not bootstrap_stopped_environment:
                cleaned = await await_owned_operation(
                    run_environment_hook(
                        environment,
                        "cleanup",
                        timeout=request.budget.cleanup_timeout_seconds,
                    ),
                    propagate_cancellation=True,
                )
                if not cleaned:
                    raise WorkflowRunLifecycleError("workflow-owned environment cleanup failed")

        manifest_path = None
        tokens_spent = None
        session_count = None
        if request.artifact_dir is not None:
            _verify_artifact_claim(request.artifact_dir)
            manifest_path = request.artifact_dir / WORKFLOW_MANIFEST_FILENAME
            manifest = _read_manifest(manifest_path)
            tokens_spent = _manifest_count(manifest, "tokens_spent", manifest_path)
            session_count = _manifest_count(manifest, "sessions", manifest_path)
        return WorkflowRunResult(
            output=output,
            workflow_name=_workflow_name(request.workflow),
            tokens_spent=tokens_spent,
            session_count=session_count,
            artifact_dir=request.artifact_dir,
            manifest_path=manifest_path,
        )

    async def run_agent(self, request: AgentRunRequest) -> AgentRunResult:
        if not isinstance(request, AgentRunRequest):
            raise TypeError("request must be an AgentRunRequest")
        workdir, source_root = _workspace_paths(request)
        owned = request.environment is None
        environment: ExecutionEnvironment = request.environment or LocalEnvironment(workdir or source_root or ".")
        if request.artifact_dir is not None:
            _claim_artifact_dir(request.artifact_dir)
        transcript_path = None if request.artifact_dir is None else request.artifact_dir / "agent.json"
        tracer = None
        trace_path = None
        if request.artifact_dir is not None and request.trace:
            tracer = Tracer(run_id=request.name, output_dir=str(request.artifact_dir), filename="trajectory.jsonl")
            trace_path = Path(tracer.path)
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
        try:
            try:
                internal = await _run_bootstrap_agent(
                    agent=agent,
                    environment=environment,
                    prompt=request.prompt,
                    max_tokens=request.budget.max_tokens,
                    max_steps=request.budget.max_steps,
                    timeout_seconds=request.budget.timeout_seconds,
                    cleanup_timeout_seconds=request.budget.cleanup_timeout_seconds,
                    transcript_path=None if transcript_path is None else str(transcript_path),
                    tracer=tracer,
                    llm=request.llm,
                    llm_timeout_seconds=config.llm_timeout_seconds,
                    cleanup_environment=owned,
                )
            except AgentRuntimeLifecycleError as exc:
                raise AgentRunLifecycleError(str(exc)) from exc
            _require_agent_evidence(
                internal,
                request.artifact_dir,
                transcript_path,
                tracer,
            )
            return _public_agent_result(request, internal, transcript_path, trace_path)
        finally:
            if tracer is not None:
                tracer.close()


def _workspace_paths(request: WorkflowRunRequest | AgentRunRequest) -> tuple[str | None, str | None]:
    workdir = request.environment_workdir or request.workspace
    if workdir is None and request.environment is not None:
        workdir = getattr(request.environment, "workspace", None)
    source_root = request.source_root or request.workspace
    if source_root is None and request.environment is not None:
        source_root = getattr(request.environment, "source_workspace", None)
    return workdir, source_root


def _claim_artifact_dir(artifact_dir: Path) -> None:
    ensure_directory_no_symlinks(artifact_dir)
    if any(artifact_dir.iterdir()):
        raise InvalidSDKRequestError("artifact_dir already contains run evidence or is claimed")
    try:
        create_regular_bytes_atomic(
            artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME,
            _SDK_ARTIFACT_CLAIM,
            max_bytes=len(_SDK_ARTIFACT_CLAIM),
        )
    except FileExistsError as exc:
        raise InvalidSDKRequestError("artifact_dir is already claimed by an SDK run") from exc


def _verify_artifact_claim(artifact_dir: Path) -> None:
    claim_path = artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME
    try:
        value = read_regular_text(claim_path, max_bytes=len(_SDK_ARTIFACT_CLAIM))
    except (OSError, UnicodeError) as exc:
        raise WorkflowManifestError(f"cannot verify SDK artifact claim: {claim_path}") from exc
    if value.encode("ascii") != _SDK_ARTIFACT_CLAIM:
        raise WorkflowManifestError(f"SDK artifact claim changed during execution: {claim_path}")


def _require_agent_evidence(
    result: AgentRuntimeResult,
    artifact_dir: Path | None,
    transcript_path: Path | None,
    tracer: Tracer | None,
) -> None:
    if not result.cleanup_quiesced or result.persistence_errors:
        raise AgentRunLifecycleError("agent cleanup or final transcript persistence failed")
    if artifact_dir is not None:
        _verify_artifact_claim(artifact_dir)
        try:
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


def _public_agent_result(
    request: AgentRunRequest,
    result: AgentRuntimeResult,
    transcript_path: Path | None,
    trace_path: Path | None,
) -> AgentRunResult:
    error = result.error
    if result.outcome == "timed_out":
        error = AgentRunTimeoutError(f"agent exceeded {request.budget.timeout_seconds:g} seconds")
    if error is not None and request.failure_mode == "raise":
        raise error
    return AgentRunResult(
        output=result.output,
        outcome=result.outcome,
        phase=result.phase,
        terminal_reason=result.terminal_reason,
        error_type=None if error is None else type(error).__name__,
        error_message=None if error is None else str(error),
        tokens_spent=result.tokens_spent,
        step_count=result.step_count,
        artifact_dir=request.artifact_dir,
        transcript_path=transcript_path,
        trace_path=trace_path,
        cleanup_quiesced=result.cleanup_quiesced,
    )


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
        raise WorkflowManifestError(f"cannot read workflow manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowManifestError(f"workflow manifest is not an object: {path}")
    return payload


def _manifest_count(manifest: dict[str, Any], key: str, path: Path) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowManifestError(f"workflow manifest has invalid {key}: {path}")
    return value


__all__ = ["OpenCollabRuntime"]
