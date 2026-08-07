"""Bootstrap wiring and public entry points for the workflow engine."""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.application.async_timeout import (
    await_owned_operation,
    force_task_terminal,
)
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.bootstrap._workflow_runtime_discovery import (
    discover_workflows,
    load_workflow_specs,
)
from opencollab.bootstrap._workflow_runtime_execution import (
    run_workflow as _run_workflow_with_integrity,
)
from opencollab.bootstrap._workflow_runtime_session import (
    _WORKFLOW_ENV_OVERRIDE,
    WorkflowSessionFactory,
    build_workflow_context,
)
from opencollab.bootstrap._workflow_runtime_state import (
    DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    WORKFLOW_AGENT_PROMPT,
    WorkflowRuntimeResult,
)
from opencollab.bootstrap.agent_runtime import revoke_and_abort_environment
from opencollab.bootstrap.session_factory import build_session

# The integrity runtime's own bounded cleanup can legitimately span several
# cleanup_timeout phases (session quiesce runs two passes, each bounded by its
# persistence and environment-abort sub-phases). Give the owner that full
# envelope to self-finish before escalating to the injected-environment fallback
# and force-termination, so a slow-but-legitimate teardown is not force-cancelled
# into a spurious WorkflowLifecycleError.
_OWNER_CLEANUP_GRACE_PHASES = 4


class WorkflowDeadlineExceeded(TimeoutError):
    """The caller-owned workflow wall-clock deadline expired."""

    def __init__(
        self,
        message: str,
        *,
        result: WorkflowRuntimeResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result


class WorkflowLifecycleError(RuntimeError):
    """Workflow-owned work failed to reach a terminal state."""


async def _wait_for_owner(
    owner: asyncio.Task[Any],
    *,
    timeout: float,
) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
    return await asyncio.wait({owner}, timeout=timeout)


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
    env: Any | None = None,
    cleanup_timeout: float = DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    source_root: str | None = None,
    deadline_monotonic: float | None = None,
    deadline_margin_seconds: float = 120.0,
    return_details: bool = False,
    cleanup_environment: bool | None = None,
) -> Any:
    """Run through one owned lifecycle with an optional wall-clock deadline."""
    if cleanup_environment is None:
        cleanup_environment = env is None
    token = _WORKFLOW_ENV_OVERRIDE.set(env)
    cancelled_result: WorkflowRuntimeResult | None = None
    cancelled_notes: tuple[str, ...] = ()

    async def run_owner() -> Any:
        nonlocal cancelled_result, cancelled_notes
        try:
            return await _run_workflow_with_integrity(
                spec_or_fn,
                args,
                cfg=cfg,
                workspace=workspace,
                tracer=tracer,
                event_sink=event_sink,
                budget=budget,
                max_concurrency=max_concurrency,
                max_steps=max_steps,
                system_prompt=system_prompt,
                save_dir=save_dir,
                trace=trace,
                cleanup_timeout=cleanup_timeout,
                env=env,
                source_root=source_root,
                deadline_monotonic=deadline_monotonic,
                deadline_margin_seconds=deadline_margin_seconds,
                return_details=return_details,
                cleanup_environment=cleanup_environment,
            )
        except asyncio.CancelledError as cancellation:
            attached = getattr(cancellation, "runtime_result", None)
            if isinstance(attached, WorkflowRuntimeResult):
                cancelled_result = attached
            cancelled_notes = tuple(getattr(cancellation, "__notes__", ()))
            raise

    owner = asyncio.create_task(run_owner())
    stop_task: asyncio.Task[tuple[bool, bool]] | None = None

    async def stop_owned(cancellation: asyncio.CancelledError) -> tuple[bool, bool]:
        if not owner.done():
            owner.cancel(*cancellation.args)

        # The integrity runtime already owns session quiescence, persistence,
        # environment abort, and diagnostic notes. Give that lifecycle its full
        # multi-phase cleanup envelope to finish before using the
        # injected-environment fallback.
        done, _pending = await asyncio.wait(
            {owner}, timeout=cleanup_timeout * _OWNER_CLEANUP_GRACE_PHASES
        )
        aborted = True
        if cleanup_environment:
            aborted = env is None or bool(getattr(env, "revoked", False))
            if not aborted:
                aborted = await revoke_and_abort_environment(env, cleanup_timeout)
        if done:
            return aborted, True
        terminal = await force_task_terminal(owner, timeout=cleanup_timeout)
        return aborted, terminal

    async def stop_once(
        cancellation: asyncio.CancelledError,
        *,
        propagate_cancellation: bool,
    ) -> tuple[bool, bool]:
        nonlocal stop_task
        if stop_task is None:
            stop_task = asyncio.create_task(stop_owned(cancellation))
        return await await_owned_operation(
            stop_task,
            propagate_cancellation=propagate_cancellation,
        )

    try:
        if deadline_monotonic is None:
            return await asyncio.shield(owner)
        remaining = max(0.0, deadline_monotonic - asyncio.get_running_loop().time())
        done, pending = await _wait_for_owner(owner, timeout=remaining)
        if not pending:
            return owner.result()
        deadline_cancellation = asyncio.CancelledError(
            "workflow wall-clock deadline expired"
        )
        aborted, terminated = await stop_once(
            deadline_cancellation,
            propagate_cancellation=True,
        )
        if not aborted or not terminated:
            raise WorkflowLifecycleError("timed-out workflow did not reach a quiescent terminal state")
        deadline_result = cancelled_result
        try:
            late_result = owner.result()
            if isinstance(late_result, WorkflowRuntimeResult):
                deadline_result = late_result
        except asyncio.CancelledError as inner:
            attached = getattr(inner, "runtime_result", None)
            if deadline_result is None and isinstance(attached, WorkflowRuntimeResult):
                deadline_result = attached
            notes = cancelled_notes or tuple(getattr(inner, "__notes__", ()))
            if notes:
                failure = WorkflowLifecycleError(
                    "timed-out workflow reported a cleanup or persistence failure"
                )
                for note in notes:
                    add_exception_note(failure, note)
                raise failure from inner
        except BaseException as inner:
            raise WorkflowLifecycleError(
                "timed-out workflow failed while reaching its terminal state"
            ) from inner
        raise WorkflowDeadlineExceeded(
            "workflow wall-clock deadline expired",
            result=deadline_result,
        )
    except asyncio.CancelledError as cancellation:
        await stop_once(cancellation, propagate_cancellation=False)
        if owner.done():
            try:
                owner.result()
            except asyncio.CancelledError as inner:
                for note in getattr(inner, "__notes__", ()):
                    add_exception_note(cancellation, note)
            except BaseException as inner:
                add_exception_note(
                    cancellation,
                    "workflow owner also failed during cancellation: "
                    f"{type(inner).__name__}: {inner}"
                )
        raise cancellation
    finally:
        _WORKFLOW_ENV_OVERRIDE.reset(token)

__all__ = [
    "WORKFLOW_AGENT_PROMPT",
    "WorkflowDeadlineExceeded",
    "WorkflowLifecycleError",
    "WorkflowRuntimeResult",
    "WorkflowSessionFactory",
    "build_session",
    "build_workflow_context",
    "discover_workflows",
    "load_workflow_specs",
    "run_workflow",
]
