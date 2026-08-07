"""Workflow cleanup, final persistence, and trace diagnostics."""

from __future__ import annotations

import asyncio
import math
from typing import Any, Literal, TypeVar

from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import TracePort
from opencollab.application.session_lifecycle import close_session_resources
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap._workflow_runtime_manifest import (
    _workflow_manifest_payload,
    _write_workflow_manifest,
)
from opencollab.bootstrap.agent_runtime import revoke_and_abort_environment

T = TypeVar("T")


def _add_failure_note(error: BaseException, note: str) -> None:
    add_exception_note(error, note)


def _merge_failure(
    primary: BaseException | None,
    secondary: BaseException | None,
    *,
    note_prefix: str,
) -> BaseException | None:
    if primary is None:
        return secondary
    if secondary is not None:
        _add_failure_note(primary, f"{note_prefix}: {type(secondary).__name__}: {secondary}")
    return primary


def _close_tracer_capture(tracer: TracePort | None) -> BaseException | None:
    close = getattr(tracer, "close", None)
    if not callable(close):
        return None
    try:
        close()
    except BaseException as exc:
        return exc
    return None


def _inspect_tracer(tracer: TracePort | None) -> tuple[str | None, int, BaseException | None]:
    if tracer is None:
        return None, 0, None
    try:
        raw_error = getattr(tracer, "write_error", None)
        write_error = str(raw_error) if raw_error else None
        dropped_steps = int(getattr(tracer, "dropped_steps", 0) or 0)
        if dropped_steps < 0:
            raise ValueError("dropped_steps must be non-negative")
    except BaseException as exc:
        return None, 0, RuntimeError(
            f"workflow tracer diagnostics could not be inspected: {type(exc).__name__}: {exc}"
        )
    return write_error, dropped_steps, None


def _sticky_tracer_failure(write_error: str | None, dropped_steps: int) -> BaseException | None:
    if not write_error:
        return None
    return OSError(f"trajectory write failed after dropping {dropped_steps} step(s): {write_error}")


def _positive_cleanup_timeout(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("cleanup_timeout must be a finite positive number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup_timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("cleanup_timeout must be a finite positive number")
    return timeout


async def _wait_for_context_cleanup(ctx: WorkflowContext, *, timeout: float) -> bool:
    """Wait once for the cleanup tasks currently owned by the context."""
    pending = set(ctx.pending_cleanup_tasks)
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    return not pending and not ctx.pending_cleanup_tasks


def _session_environments(ctx: WorkflowContext) -> tuple[Any, ...]:
    environments: list[Any] = []
    seen: set[int] = set()
    for session in ctx.sessions:
        environment = getattr(session, "env", None)
        if environment is None:
            environment = getattr(getattr(session, "tool_execution", None), "environment", None)
        if environment is not None and id(environment) not in seen:
            seen.add(id(environment))
            environments.append(environment)
    return tuple(environments)


def _session_persistence_succeeded(ctx: WorkflowContext) -> bool:
    return all(not getattr(session, "persistence_errors", ()) for session in ctx.sessions)


async def _abort_session_environments(ctx: WorkflowContext, *, timeout: float) -> bool:
    for task in ctx.pending_cleanup_tasks:
        task.cancel()
    if not getattr(ctx, "_cleanup_environment", True):
        return True
    environments = _session_environments(ctx)
    if not environments:
        return True
    results = await asyncio.gather(
        *(revoke_and_abort_environment(environment, timeout) for environment in environments),
        return_exceptions=True,
    )
    return all(result is True for result in results)


async def _quiesce_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    if await _wait_for_context_cleanup(ctx, timeout=timeout):
        return True, _session_persistence_succeeded(ctx), ()
    abort_succeeded = await _abort_session_environments(ctx, timeout=timeout)
    quiesced = await _wait_for_context_cleanup(ctx, timeout=timeout)
    lingering = tuple(ctx.pending_cleanup_tasks)
    return (
        quiesced,
        abort_succeeded and quiesced and _session_persistence_succeeded(ctx),
        lingering,
    )


async def _quiesce_and_finalize_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    quiesced, succeeded, lingering = await _quiesce_workflow_context(ctx, timeout=timeout)
    if not quiesced:
        return quiesced, succeeded, lingering
    enqueued = True
    for session in ctx.sessions:
        enqueue = getattr(session, "enqueue_auto_save", None)
        if not callable(enqueue):
            continue
        try:
            enqueue()
        except Exception:
            enqueued = False
    final_quiesced, final_succeeded, final_lingering = await _quiesce_workflow_context(
        ctx,
        timeout=timeout,
    )
    resources_closed = await close_session_resources(ctx.sessions, timeout=timeout)
    return (
        final_quiesced,
        succeeded and enqueued and final_succeeded and resources_closed,
        (*lingering, *final_lingering),
    )


async def _await_owned_with_cancellation(
    task: asyncio.Task[T],
) -> tuple[T, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            if task.done() and task.cancelled():
                raise
            cancellation = cancellation or exc


def _persist_workflow_manifest(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    status: Literal["completed", "stopped", "failed"],
    reason: str | None,
    failure_type: str | None,
    evidence_complete: bool,
) -> Exception | None:
    try:
        manifest = _workflow_manifest_payload(
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            status=status,
            reason=reason,
            failure_type=failure_type,
            evidence_complete=evidence_complete,
        )
        _write_workflow_manifest(
            save_dir,
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            status=status,
            reason=reason,
            failure_type=failure_type,
            evidence_complete=evidence_complete,
            manifest=manifest,
        )
    except Exception as exc:
        return exc
    return None
