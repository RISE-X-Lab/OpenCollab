"""Workflow quiescence, manifest ownership, and deferred tracer cleanup."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
from collections.abc import Sequence
from typing import Any

from opencollab.application.async_timeout import isolate_tasks_from_shutdown
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import TracePort
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap._workflow_runtime_manifest import (
    WORKFLOW_MANIFEST_FILENAME,
    _workflow_manifest_payload,
    _write_workflow_manifest,
)
from opencollab.bootstrap._workflow_runtime_state import (
    _LATE_TRACER_FAILURES,
    _LATE_TRACER_OWNER_TASKS,
    _WORKFLOW_MANIFEST_OWNER_TASKS,
)


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
        _add_failure_note(
            primary,
            f"{note_prefix}: {type(secondary).__name__}: {secondary}",
        )
    return primary


def _close_tracer_capture(tracer: TracePort) -> BaseException | None:
    close = getattr(tracer, "close", None)
    if not callable(close):
        return None
    try:
        close()
    except BaseException as exc:
        return exc
    return None


def _inspect_tracer(
    tracer: TracePort | None,
) -> tuple[str | None, int, BaseException | None]:
    if tracer is None:
        return None, 0, None
    try:
        raw_write_error = getattr(tracer, "write_error", None)
        write_error = str(raw_write_error) if raw_write_error else None
        dropped_steps = int(getattr(tracer, "dropped_steps", 0) or 0)
        if dropped_steps < 0:
            raise ValueError("dropped_steps must be non-negative")
    except BaseException as exc:
        return (
            None,
            0,
            RuntimeError(f"workflow tracer diagnostics could not be inspected: {type(exc).__name__}: {exc}"),
        )
    return write_error, dropped_steps, None


def _sticky_tracer_failure(
    write_error: str | None,
    dropped_steps: int,
) -> BaseException | None:
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


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _wait_for_context_cleanup(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> bool:
    """Wait through one stable empty turn for all context-owned cleanup."""
    deadline = asyncio.get_running_loop().time() + timeout
    saw_empty = False
    while True:
        pending = set(ctx.pending_cleanup_tasks)
        if not pending:
            if saw_empty:
                return True
            saw_empty = True
            await asyncio.sleep(0)
            continue
        saw_empty = False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        _done, still_pending = await asyncio.wait(pending, timeout=remaining)
        if still_pending:
            return False


def _session_environments(ctx: WorkflowContext) -> tuple[Any, ...]:
    environments: list[Any] = []
    seen: set[int] = set()
    for session in ctx.sessions:
        environment = getattr(session, "env", None)
        if environment is None:
            tool_execution = getattr(session, "tool_execution", None)
            environment = getattr(tool_execution, "environment", None)
        if environment is None or id(environment) in seen:
            continue
        seen.add(id(environment))
        environments.append(environment)
    return tuple(environments)


def _session_persistence_succeeded(ctx: WorkflowContext) -> bool:
    return all(not getattr(session, "persistence_errors", ()) for session in ctx.sessions)


async def _abort_session_environments(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    """Synchronously revoke session environments, then bound their abort hooks."""
    abort_tasks: set[asyncio.Future[Any]] = set()
    succeeded = True
    for environment in _session_environments(ctx):
        try:
            setattr(environment, "_aborted", True)
        except Exception:
            succeeded = False
        abort = getattr(environment, "abort", None)
        if not callable(abort):
            continue
        try:
            outcome = abort()
        except Exception:
            succeeded = False
            continue
        if not inspect.isawaitable(outcome):
            continue
        try:
            abort_tasks.add(asyncio.ensure_future(outcome))
        except Exception:
            succeeded = False
            close = getattr(outcome, "close", None)
            if callable(close):
                close()

    for task in ctx.pending_cleanup_tasks:
        task.cancel()

    if not abort_tasks:
        return True, succeeded, ()
    done, pending = await asyncio.wait(abort_tasks, timeout=timeout)
    for task in done:
        try:
            task.result()
        except BaseException:
            succeeded = False
    for task in pending:
        task.cancel()
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    await isolate_tasks_from_shutdown(pending, timeout=timeout)
    abort_quiesced = not pending
    return abort_quiesced, succeeded and abort_quiesced, tuple(pending)


async def _quiesce_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    if await _wait_for_context_cleanup(ctx, timeout=timeout):
        return True, _session_persistence_succeeded(ctx), ()
    abort_quiesced, abort_succeeded, lingering_abort_tasks = await _abort_session_environments(
        ctx,
        timeout=timeout,
    )
    cleanup_quiesced = await _wait_for_context_cleanup(ctx, timeout=timeout)
    all_quiesced = abort_quiesced and cleanup_quiesced
    return (
        all_quiesced,
        (abort_succeeded and all_quiesced and _session_persistence_succeeded(ctx)),
        lingering_abort_tasks,
    )


async def _quiesce_and_finalize_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    """Quiesce execution, enqueue final snapshots, then quiesce persistence."""
    quiesced, succeeded, lingering = await _quiesce_workflow_context(
        ctx,
        timeout=timeout,
    )
    if not quiesced:
        return quiesced, succeeded, lingering

    enqueue_succeeded = True
    for session in ctx.sessions:
        enqueue = getattr(session, "enqueue_auto_save", None)
        if not callable(enqueue):
            continue
        try:
            enqueue()
        except Exception:
            enqueue_succeeded = False

    final_quiesced, final_succeeded, final_lingering = await _quiesce_workflow_context(
        ctx,
        timeout=timeout,
    )
    return (
        quiesced and final_quiesced,
        succeeded and enqueue_succeeded and final_succeeded,
        (*lingering, *final_lingering),
    )


async def _await_cleanup_despite_cancellation(
    cleanup_task: asyncio.Task[tuple[bool, bool, tuple[asyncio.Future[Any], ...]]],
) -> tuple[
    tuple[bool, bool, tuple[asyncio.Future[Any], ...]],
    asyncio.CancelledError | None,
]:
    """Keep one owned cleanup task alive through repeated caller cancellation."""
    first_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(cleanup_task), first_cancellation
        except asyncio.CancelledError as exc:
            if cleanup_task.done() and cleanup_task.cancelled():
                raise
            if first_cancellation is None:
                first_cancellation = exc


def _workflow_manifest_owner_done(task: asyncio.Task[Any]) -> None:
    _WORKFLOW_MANIFEST_OWNER_TASKS.discard(task)
    _consume_task_result(task)


async def _await_manifest_daemon_write(write: Any) -> None:
    await asyncio.wrap_future(write)


def _track_manifest_daemon_writes(
    subscriber: AutoSaveSubscriber,
) -> set[asyncio.Task[Any]]:
    owners: set[asyncio.Task[Any]] = set()
    for write in subscriber.pending_write_futures:
        owner = asyncio.create_task(_await_manifest_daemon_write(write))
        _WORKFLOW_MANIFEST_OWNER_TASKS.add(owner)
        owner.add_done_callback(_workflow_manifest_owner_done)
        owners.add(owner)
    return owners


async def _persist_workflow_manifest_owned(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    timeout: float,
) -> tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]:
    """Persist one frozen manifest through an owned, bounded worker task."""
    manifest = _workflow_manifest_payload(
        name=name,
        args=args,
        ctx=ctx,
        tracer=tracer,
        tracer_failure=tracer_failure,
        tracer_write_error=tracer_write_error,
        tracer_dropped_steps=tracer_dropped_steps,
    )
    subscriber = AutoSaveSubscriber(
        lambda: _write_workflow_manifest(
            save_dir,
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            manifest=manifest,
        ),
        serialization_key=os.path.join(
            save_dir,
            WORKFLOW_MANIFEST_FILENAME,
        ),
    )
    owner = subscriber.enqueue()
    if owner is None:
        return True, subscriber.last_error, ()

    _WORKFLOW_MANIFEST_OWNER_TASKS.add(owner)
    owner.add_done_callback(_workflow_manifest_owner_done)
    pending: set[asyncio.Task[Any]] = {owner}
    _done, pending = await asyncio.wait(pending, timeout=timeout)
    if pending:
        for task in pending:
            task.cancel()
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    await isolate_tasks_from_shutdown(pending, timeout=timeout)
    write_owners = _track_manifest_daemon_writes(subscriber)
    if write_owners:
        _done, write_owners = await asyncio.wait(write_owners, timeout=0)
        pending.update(write_owners)
    return not pending, subscriber.last_error, tuple(pending)


async def _await_manifest_despite_cancellation(
    manifest_task: asyncio.Task[tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]],
) -> tuple[
    tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]],
    asyncio.CancelledError | None,
]:
    """Keep manifest ownership alive through repeated caller cancellation."""
    first_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(manifest_task), first_cancellation
        except asyncio.CancelledError as exc:
            if manifest_task.done() and manifest_task.cancelled():
                raise
            if first_cancellation is None:
                first_cancellation = exc


async def _wait_for_late_quiescence(
    ctx: WorkflowContext,
    extra_tasks: Sequence[asyncio.Future[Any]],
    *,
    timeout: float,
) -> bool:
    """Wait through loop-shutdown cancellation after the boundary reported."""
    extras = set(extra_tasks)
    deadline = asyncio.get_running_loop().time() + timeout
    saw_empty = False
    while True:
        pending = {task for task in extras if not task.done()}
        pending.update(ctx.pending_cleanup_tasks)
        current = asyncio.current_task()
        if current is not None:
            pending.discard(current)
        if not pending:
            if saw_empty:
                return True
            saw_empty = True
            await asyncio.sleep(0)
            continue
        saw_empty = False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await isolate_tasks_from_shutdown(pending, timeout=1e-6)
            return False
        waiter = asyncio.create_task(asyncio.wait(pending, timeout=remaining))
        while True:
            try:
                await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    break
                continue
        _done, still_pending = waiter.result()
        if still_pending and asyncio.get_running_loop().time() >= deadline:
            await isolate_tasks_from_shutdown(still_pending, timeout=1e-6)
            return False
        extras.update(still_pending)


async def _close_tracer_after_late_cleanup(
    ctx: WorkflowContext,
    tracer: TracePort,
    extra_tasks: Sequence[asyncio.Future[Any]],
    *,
    timeout: float,
) -> None:
    quiesced = False
    try:
        quiesced = await _wait_for_late_quiescence(
            ctx,
            extra_tasks,
            timeout=timeout,
        )
    finally:
        if not quiesced:
            _LATE_TRACER_FAILURES.append(
                TimeoutError("late workflow tracer dependencies did not quiesce before their final deadline")
            )
        try:
            tracer.close()
        except BaseException as exc:
            _LATE_TRACER_FAILURES.append(exc)
            raise


def _late_tracer_owner_done(task: asyncio.Task[Any]) -> None:
    _LATE_TRACER_OWNER_TASKS.discard(task)
    _consume_task_result(task)


def _defer_owned_tracer_close(
    ctx: WorkflowContext,
    tracer: TracePort,
    extra_tasks: Sequence[asyncio.Future[Any]],
) -> None:
    late_timeout = 2.0
    owner = asyncio.create_task(
        _close_tracer_after_late_cleanup(
            ctx,
            tracer,
            extra_tasks,
            timeout=late_timeout,
        )
    )
    _LATE_TRACER_OWNER_TASKS.add(owner)
    owner.add_done_callback(_late_tracer_owner_done)
