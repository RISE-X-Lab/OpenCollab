"""Workflow entry-point execution and failure arbitration."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from opencollab.adapters.trace import Tracer
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.application.workflow import WorkflowBudgetExceeded
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap._workflow_runtime_cleanup import (
    _add_failure_note,
    _await_owned_with_cancellation,
    _close_tracer_capture,
    _inspect_tracer,
    _merge_failure,
    _persist_workflow_manifest,
    _positive_cleanup_timeout,
    _quiesce_and_finalize_workflow_context,
    _sticky_tracer_failure,
)
from opencollab.bootstrap._workflow_runtime_session import _resolve_spec_fn, build_workflow_context
from opencollab.bootstrap._workflow_runtime_state import (
    DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    WORKFLOW_AGENT_PROMPT,
    WorkflowRuntimeResult,
)
from opencollab.bootstrap.session_factory import ORCHESTRATION_FILENAME


def _note_failure(primary: BaseException, label: str, failure: BaseException | None) -> None:
    if failure is not None:
        _add_failure_note(primary, f"{label}: {type(failure).__name__}: {failure}")


def _runtime_result(
    ctx: Any,
    *,
    output: Any,
    name: str,
    stop_reason: Literal["budget_exceeded"] | None,
    evidence_complete: bool,
) -> WorkflowRuntimeResult:
    sessions = tuple(ctx.sessions)
    return WorkflowRuntimeResult(
        output=output,
        name=name,
        tokens=ctx.budget.spent(),
        sessions=len(sessions),
        steps=sum(int(getattr(session, "step_count", 0)) for session in sessions),
        markup_recovered=sum(
            int(getattr(session, "markup_recovered", 0))
            for session in sessions
        ),
        agent_failures=ctx.agent_failures,
        stop_reason=stop_reason,
        evidence_complete=evidence_complete,
    )


def _attach_runtime_result(
    error: BaseException | None,
    result: WorkflowRuntimeResult,
) -> None:
    if error is not None:
        error.runtime_result = result  # type: ignore[attr-defined]


async def run_workflow(
    spec_or_fn: Any,
    args: dict[str, Any],
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    max_steps: int = 100,
    system_prompt: str = WORKFLOW_AGENT_PROMPT,
    save_dir: str | None = None,
    trace: bool = True,
    cleanup_timeout: float = DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    env: Any | None = None,
    source_root: str | None = None,
    deadline_monotonic: float | None = None,
    deadline_margin_seconds: float = 120.0,
    return_details: bool = False,
) -> Any:
    """Run one workflow and return only after cleanup and evidence persistence."""
    cleanup_timeout = _positive_cleanup_timeout(cleanup_timeout)
    fn = _resolve_spec_fn(spec_or_fn)
    metadata = (
        spec_or_fn
        if isinstance(spec_or_fn, WorkflowSpec)
        else getattr(fn, "__workflow_spec__", None)
    )
    name = (
        metadata.name
        if isinstance(metadata, WorkflowSpec)
        else getattr(fn, "__name__", "workflow")
    )
    owns_tracer = tracer is None and save_dir is not None and trace
    if owns_tracer:
        tracer = Tracer(run_id=name, output_dir=save_dir, filename=ORCHESTRATION_FILENAME)

    try:
        ctx = build_workflow_context(
            cfg=cfg,
            workspace=workspace,
            tracer=tracer,
            event_sink=event_sink,
            budget=budget,
            max_concurrency=max_concurrency,
            max_steps=max_steps,
            system_prompt=system_prompt,
            save_dir=save_dir,
            env=env,
            source_root=source_root,
            deadline_monotonic=deadline_monotonic,
            deadline_margin_seconds=deadline_margin_seconds,
        )
    except BaseException as exc:
        close_failure = _close_tracer_capture(tracer) if owns_tracer else None
        _note_failure(exc, "owned workflow tracer close also failed", close_failure)
        raise

    result: Any = None
    stop_reason: Literal["budget_exceeded"] | None = None
    workflow_failure: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None
    try:
        result = await fn(ctx, args)
    except WorkflowBudgetExceeded as exc:
        stop_reason = "budget_exceeded"
        result = {
            "status": "budget_exceeded",
            "error": str(exc),
            "tokens_spent": ctx.budget.spent(),
            "budget_total": ctx.budget.total,
        }
    except BaseException as exc:
        workflow_failure = exc
        if isinstance(exc, asyncio.CancelledError):
            cancellation = exc

    cleanup_failure: BaseException | None = None
    cleanup_succeeded = False
    cleanup_task = asyncio.create_task(
        _quiesce_and_finalize_workflow_context(ctx, timeout=cleanup_timeout)
    )
    try:
        cleanup_result, cleanup_cancellation = await _await_owned_with_cancellation(cleanup_task)
        _cleanup_quiesced, cleanup_succeeded, _lingering = cleanup_result
        cancellation = cancellation or cleanup_cancellation
    except BaseException as exc:
        cleanup_failure = exc

    tracer_failure = _close_tracer_capture(tracer) if owns_tracer else None
    tracer_write_error, tracer_dropped_steps, inspection_failure = _inspect_tracer(tracer)
    tracer_failure = _merge_failure(
        tracer_failure,
        inspection_failure or _sticky_tracer_failure(tracer_write_error, tracer_dropped_steps),
        note_prefix="workflow trace also failed",
    )

    if cancellation is not None:
        terminal_status: Literal["completed", "stopped", "failed"] = "stopped"
        terminal_reason = "cancelled"
        failure_type = type(cancellation).__name__
    elif workflow_failure is not None:
        terminal_status = "failed"
        terminal_reason = "workflow_exception"
        failure_type = type(workflow_failure).__name__
    elif stop_reason is not None:
        terminal_status = "stopped"
        terminal_reason = stop_reason
        failure_type = None
    else:
        terminal_status = "completed"
        terminal_reason = None
        failure_type = None
    requested_manifest = save_dir is not None
    pre_manifest_evidence_complete = (
        cleanup_failure is None
        and cleanup_succeeded
        and tracer_failure is None
    )
    manifest_error = None
    if (
        requested_manifest
        and cleanup_failure is None
        and cleanup_succeeded
    ):
        manifest_error = _persist_workflow_manifest(
            save_dir,
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            status=terminal_status,
            reason=terminal_reason,
            failure_type=failure_type,
            evidence_complete=pre_manifest_evidence_complete,
        )

    evidence_complete = (
        pre_manifest_evidence_complete
        and (not requested_manifest or manifest_error is None)
    )
    runtime_result = _runtime_result(
        ctx,
        output=result,
        name=name,
        stop_reason=stop_reason,
        evidence_complete=evidence_complete,
    )
    _attach_runtime_result(cancellation, runtime_result)
    _attach_runtime_result(workflow_failure, runtime_result)

    if cancellation is not None:
        _note_failure(cancellation, "workflow cleanup also failed", cleanup_failure)
        if cleanup_failure is None and not cleanup_succeeded:
            _add_failure_note(
                cancellation,
                "workflow cleanup also failed: final snapshot or environment abort failed",
            )
        _note_failure(cancellation, "workflow manifest persistence also failed", manifest_error)
        _note_failure(cancellation, "workflow trace also failed", tracer_failure)
        raise cancellation
    if cleanup_failure is not None:
        failure = RuntimeError("technical workflow cleanup failed")
        _note_failure(failure, "workflow execution also failed", workflow_failure)
        _note_failure(failure, "workflow manifest persistence also failed", manifest_error)
        _note_failure(failure, "workflow trace also failed", tracer_failure)
        raise failure from cleanup_failure
    if not cleanup_succeeded:
        failure = RuntimeError("technical workflow cleanup failed: final snapshot or environment abort failed")
        _note_failure(failure, "workflow manifest persistence also failed", manifest_error)
        _note_failure(failure, "workflow trace also failed", tracer_failure)
        raise failure from workflow_failure
    if workflow_failure is not None:
        _note_failure(workflow_failure, "workflow manifest persistence also failed", manifest_error)
        _note_failure(workflow_failure, "workflow trace also failed", tracer_failure)
        raise workflow_failure
    if manifest_error is not None:
        failure = RuntimeError("technical workflow manifest persistence failed")
        _note_failure(failure, "workflow trace also failed", tracer_failure)
        raise failure from manifest_error
    if tracer_failure is not None:
        raise RuntimeError("technical workflow trace failed: orchestration evidence is incomplete") from tracer_failure
    if return_details:
        return runtime_result
    return result
