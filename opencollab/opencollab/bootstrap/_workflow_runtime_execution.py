"""Workflow entry-point execution and failure arbitration."""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.adapters.trace import Tracer
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.application.workflow import WorkflowBudgetExceeded
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap._workflow_runtime_cleanup import (
    _add_failure_note,
    _await_cleanup_despite_cancellation,
    _await_manifest_despite_cancellation,
    _close_tracer_capture,
    _defer_owned_tracer_close,
    _inspect_tracer,
    _merge_failure,
    _persist_workflow_manifest_owned,
    _positive_cleanup_timeout,
    _quiesce_and_finalize_workflow_context,
    _sticky_tracer_failure,
)
from opencollab.bootstrap._workflow_runtime_session import (
    _resolve_spec_fn,
    build_workflow_context,
)
from opencollab.bootstrap._workflow_runtime_state import (
    DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
)
from opencollab.bootstrap.session_factory import ORCHESTRATION_FILENAME


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
    save_dir: str | None = None,
    trace: bool = True,
    cleanup_timeout: float = DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
) -> Any:
    """Build a context, run the workflow function with ``args``, return its result.

    Accepts either a :class:`WorkflowSpec` or a raw ``@workflow``-decorated (or
    plain async) function.

    ``WorkflowBudgetExceeded`` — the sole exception ``WorkflowContext`` lets
    escape — is caught at this run boundary and turned into a structured result
    so the CLI prints a JSON budget report instead of a raw traceback::

        {"status": "budget_exceeded", "error": <str>,
         "tokens_spent": <int>, "budget_total": <int | None>}

    Every other exception still propagates to the caller.

    When ``save_dir`` is given the run folder mirrors a team run folder: each
    session's conversation is autosaved per role (``<seq>_<role>.json``) and a
    ``workflow.json`` manifest (workflow name, args, session count, spend) ties
    them together the way the team manifest groups a chat run's agents.

    A saved run also records the run's orchestration signals to a single
    ``<save_dir>/orchestration.jsonl`` (one ``workflow_phase`` / ``workflow_log``
    /  ``llm_call`` / ``tool_exec`` record per step, with tokens and latency) via
    an auto-wired :class:`Tracer` — the scheduling/step trace kept out of the
    per-role conversations. Pass ``trace=False`` to opt out, or supply your own
    ``tracer`` to keep ownership (it is then not auto-closed).

    ``cleanup_timeout`` bounds each shutdown phase. A timed-out session first
    receives a grace period, then all of its environments are synchronously
    revoked and their abort hooks are bounded. Persistence and owned-tracer
    closure happen only after every owned cleanup task is quiescent.
    """
    cleanup_timeout = _positive_cleanup_timeout(cleanup_timeout)
    fn = _resolve_spec_fn(spec_or_fn)
    name = spec_or_fn.name if isinstance(spec_or_fn, WorkflowSpec) else getattr(fn, "__name__", "workflow")

    # Own a Tracer only when saving, not opted out, and the caller didn't bring
    # one; close it in the finally below so the file handle is released even if
    # the workflow raises. A caller-supplied tracer keeps its own lifecycle. The
    # ``run_id`` is the workflow name (meaningful in each record); the on-disk
    # file is always ``orchestration.jsonl`` in the run folder.
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
            save_dir=save_dir,
        )
    except BaseException as exc:
        if owns_tracer:
            tracer_close_failure = _close_tracer_capture(tracer)
            if tracer_close_failure is not None:
                _add_failure_note(
                    exc,
                    "owned workflow tracer close also failed: "
                    f"{type(tracer_close_failure).__name__}: "
                    f"{tracer_close_failure}",
                )
        raise
    cleanup_quiesced = False
    cleanup_succeeded = False
    lingering_cleanup_tasks: tuple[asyncio.Future[Any], ...] = ()
    workflow_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    first_cancellation: asyncio.CancelledError | None = None
    tracer_closed = False
    tracer_close_deferred = False
    tracer_failure: BaseException | None = None
    tracer_write_error: str | None = None
    tracer_dropped_steps = 0
    result: Any = None
    try:
        try:
            result = await fn(ctx, args)
        except WorkflowBudgetExceeded as exc:
            result = {
                "status": "budget_exceeded",
                "error": str(exc),
                "tokens_spent": ctx.budget.spent(),
                "budget_total": ctx.budget.total,
            }
        except BaseException as exc:
            workflow_failure = exc
            if isinstance(exc, asyncio.CancelledError):
                first_cancellation = exc

        cleanup_task = asyncio.create_task(
            _quiesce_and_finalize_workflow_context(
                ctx,
                timeout=cleanup_timeout,
            )
        )
        try:
            cleanup_result, cleanup_cancellation = await _await_cleanup_despite_cancellation(cleanup_task)
            (
                cleanup_quiesced,
                cleanup_succeeded,
                lingering_cleanup_tasks,
            ) = cleanup_result
            if first_cancellation is None:
                first_cancellation = cleanup_cancellation
        except BaseException as exc:
            cleanup_failure = exc
            lingering_cleanup_tasks = (cleanup_task,)

        (
            tracer_write_error,
            tracer_dropped_steps,
            tracer_inspection_failure,
        ) = _inspect_tracer(tracer)
        sticky_tracer_failure = _sticky_tracer_failure(
            tracer_write_error,
            tracer_dropped_steps,
        )
        if tracer_failure is None:
            tracer_failure = tracer_inspection_failure or sticky_tracer_failure

        early_exit = (
            first_cancellation is not None
            or cleanup_failure is not None
            or not cleanup_succeeded
            or workflow_failure is not None
        )
        if owns_tracer and cleanup_quiesced and early_exit:
            tracer_close_failure = _close_tracer_capture(tracer)
            tracer_closed = True
            tracer_failure = _merge_failure(
                tracer_failure,
                tracer_close_failure,
                note_prefix="workflow tracer close also failed",
            )

        if first_cancellation is not None:
            if cleanup_failure is not None:
                _add_failure_note(
                    first_cancellation,
                    f"workflow cleanup also failed: {type(cleanup_failure).__name__}: {cleanup_failure}",
                )
            elif not cleanup_succeeded:
                _add_failure_note(
                    first_cancellation,
                    "workflow cleanup also failed: session cleanup, final snapshot, or environment abort failed",
                )
            if tracer_failure is not None:
                _add_failure_note(
                    first_cancellation,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise first_cancellation
        if cleanup_failure is not None:
            failure = RuntimeError("technical workflow cleanup failed: owned cleanup task failed")
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            if workflow_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow execution also failed: {type(workflow_failure).__name__}: {workflow_failure}",
                )
            raise failure from cleanup_failure
        if not cleanup_succeeded:
            failure = RuntimeError("technical workflow cleanup failed: session cleanup or environment abort failed")
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from workflow_failure
        if workflow_failure is not None:
            if tracer_failure is not None:
                _add_failure_note(
                    workflow_failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise workflow_failure

        manifest_quiesced = True
        manifest_error: Exception | None = None
        manifest_failure: BaseException | None = None
        manifest_lingering_tasks: tuple[asyncio.Task[Any], ...] = ()
        manifest_cancellation: asyncio.CancelledError | None = None
        if save_dir is not None:
            manifest_task = asyncio.create_task(
                _persist_workflow_manifest_owned(
                    save_dir,
                    name=name,
                    args=args,
                    ctx=ctx,
                    tracer=tracer,
                    tracer_failure=tracer_failure,
                    tracer_write_error=tracer_write_error,
                    tracer_dropped_steps=tracer_dropped_steps,
                    timeout=cleanup_timeout,
                )
            )
            try:
                manifest_result, manifest_cancellation = await _await_manifest_despite_cancellation(manifest_task)
                (
                    manifest_quiesced,
                    manifest_error,
                    manifest_lingering_tasks,
                ) = manifest_result
            except BaseException as exc:
                manifest_failure = exc
                if not manifest_task.done():
                    manifest_lingering_tasks = (manifest_task,)

        if owns_tracer and cleanup_quiesced:
            if manifest_lingering_tasks:
                _defer_owned_tracer_close(
                    ctx,
                    tracer,
                    (*lingering_cleanup_tasks, *manifest_lingering_tasks),
                    timeout=cleanup_timeout,
                )
                tracer_close_deferred = True
            else:
                tracer_close_failure = _close_tracer_capture(tracer)
                tracer_closed = True
                tracer_failure = _merge_failure(
                    tracer_failure,
                    tracer_close_failure,
                    note_prefix="workflow tracer close also failed",
                )

        if manifest_cancellation is not None:
            if manifest_failure is not None:
                _add_failure_note(
                    manifest_cancellation,
                    f"workflow manifest persistence also failed: {type(manifest_failure).__name__}: {manifest_failure}",
                )
            elif not manifest_quiesced:
                _add_failure_note(
                    manifest_cancellation,
                    "workflow cleanup also failed: manifest persistence did not quiesce",
                )
            elif manifest_error is not None:
                _add_failure_note(
                    manifest_cancellation,
                    f"workflow manifest persistence also failed: {type(manifest_error).__name__}: {manifest_error}",
                )
            if tracer_failure is not None:
                _add_failure_note(
                    manifest_cancellation,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise manifest_cancellation
        if manifest_failure is not None:
            failure = RuntimeError("technical workflow manifest persistence failed: owned manifest task failed")
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from manifest_failure
        if not manifest_quiesced:
            failure = RuntimeError("technical workflow cleanup failed: workflow manifest persistence did not quiesce")
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure
        if manifest_error is not None:
            failure = RuntimeError("technical workflow manifest persistence failed")
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    f"workflow trace also failed: {type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from manifest_error
        if tracer_failure is not None:
            raise RuntimeError(
                "technical workflow trace failed: orchestration evidence is incomplete"
            ) from tracer_failure
        return result
    finally:
        if owns_tracer:
            if tracer_close_deferred:
                pass
            elif cleanup_quiesced:
                if not tracer_closed:
                    _close_tracer_capture(tracer)
            else:
                _defer_owned_tracer_close(
                    ctx,
                    tracer,
                    lingering_cleanup_tasks,
                    timeout=cleanup_timeout,
                )
