from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from opencollab.adapters.env import Environment
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.trace import Tracer
from opencollab.application.async_timeout import (
    abandon_on_timeout,
    force_task_terminal,
    isolate_tasks_from_shutdown,
)
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap.session_factory import WORKFLOW_MANIFEST_FILENAME
from opencollab.harness.swe_checkpoint import WorktreeCheckpoint

if TYPE_CHECKING:
    from opencollab.harness.evaluator import EvalTask

EnvFactory = Callable[["EvalTask"], Awaitable[Environment]]
_EVAL_MANIFEST_OWNER_TASKS: set[asyncio.Task[Any]] = set()
_LATE_EVAL_RESOURCE_TASKS: set[asyncio.Task[Any]] = set()
_LATE_EVAL_RESOURCE_FAILURES: deque[BaseException] = deque(maxlen=64)


def _evaluator_module():
    return sys.modules["opencollab.harness.evaluator"]


async def _wait_for_owned_execution(
    tasks: Sequence[asyncio.Task[Any]],
    workflow_ctx: WorkflowContext | None,
    *,
    cleanup_timeout: float,
) -> bool:
    """Bound cancellation cleanup before diff extraction and env teardown."""

    def pending_tasks() -> set[asyncio.Task[Any]]:
        pending = {task for task in tasks if not task.done()}
        if workflow_ctx is not None:
            pending.update(workflow_ctx.pending_cleanup_tasks)
        return pending

    async def wait_phase(timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        saw_empty = False
        while True:
            pending = pending_tasks()
            if not pending:
                if saw_empty:
                    return True
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _done, still_pending = await asyncio.wait(pending, timeout=remaining)
            if still_pending:
                return False

    if await wait_phase(cleanup_timeout):
        return True

    # A coroutine may consume the first CancelledError while unwinding. A
    # second cancellation interrupts that cleanup wait without making the
    # ordinary deadline depend on an unbounded provider/tool finally block.
    pending = pending_tasks()
    for task in pending:
        task.cancel()
    forced_timeout = min(2.0, max(0.1, cleanup_timeout))
    if await wait_phase(forced_timeout):
        return True
    await isolate_tasks_from_shutdown(
        pending_tasks(),
        timeout=forced_timeout,
    )
    # Non-terminal results were removed from loop-shutdown ownership. The false
    # return keeps submission eligibility conservative while teardown continues.
    return False


async def _abort_environment(
    env: Environment,
    *,
    cleanup_timeout: float,
) -> bool:
    # Revoke the public environment surface synchronously, before an adapter's
    # resource-specific abort hook gets a chance to block or consume cancel.
    env._aborted = True
    abort = getattr(env, "abort", None)
    if not callable(abort):
        return True
    task = asyncio.ensure_future(abort())
    if await _wait_for_owned_execution([task], None, cleanup_timeout=cleanup_timeout):
        if task.cancelled():
            return False
        task.result()
        return True
    task.add_done_callback(lambda finished: _consume_background_task(finished))
    return False


async def _cleanup_environment_bounded(
    env: Environment,
    *,
    cleanup_timeout: float,
) -> bool:
    cleanup_task = asyncio.ensure_future(env.cleanup())
    quiesced = await _wait_for_owned_execution(
        [cleanup_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if cleanup_task.cancelled():
            return False
        cleanup_task.result()
    if not quiesced:
        cleanup_task.add_done_callback(_consume_background_task)
    return quiesced


class _EnvironmentSetupOwner:
    """Keep setup and any late environment under one task's ownership.

    The owner task stays alive until ``run_eval_task`` either accepts the
    environment or relinquishes it.  A factory that consumes cancellation and
    returns after the caller-side deadline is therefore disposed *inside the
    already-owned setup task*.  This matters during ``asyncio.run`` shutdown:
    the runner waits for the original task, while a cleanup task created by a
    done callback would be absent from the runner's cancellation snapshot and
    could be destroyed when the loop closes.

    Each teardown operation runs in a shielded child with a fixed deadline.
    Cancellation aimed at the owner therefore cannot skip disposal, while an
    adapter that never completes becomes explicit disposal evidence instead of
    keeping the event loop alive indefinitely.
    """

    def __init__(
        self,
        factory: EnvFactory,
        eval_task: EvalTask,
        *,
        cleanup_timeout: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._factory = factory
        self._eval_task = eval_task
        self._cleanup_timeout = cleanup_timeout
        self._delivery: asyncio.Future[Environment] = loop.create_future()
        self._decision = asyncio.Event()
        self._transferred = False
        self._dispose_requested = False
        self._environment: Environment | None = None
        self.disposal_errors: list[tuple[str, BaseException]] = []
        self.task = loop.create_task(self._run())
        self.task.add_done_callback(_consume_background_task)

    async def acquire(self, timeout: float) -> Environment:
        return await abandon_on_timeout(self._delivery, timeout)

    def transfer(self, env: Environment) -> None:
        if env is not self._environment:
            raise RuntimeError("environment setup ownership transfer mismatch")
        if self._dispose_requested:
            raise RuntimeError("environment setup ownership was already relinquished")
        self._transferred = True
        self._decision.set()

    def relinquish(self) -> None:
        self._dispose_requested = True
        self._decision.set()
        if not self.task.done():
            self.task.cancel()

    async def _finish_teardown_operation(
        self,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            operation_task = asyncio.ensure_future(operation())
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))
            return

        waiter = asyncio.create_task(
            asyncio.wait(
                {operation_task},
                timeout=self._cleanup_timeout,
            )
        )
        while True:
            try:
                _done, pending = await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    _done, pending = waiter.result()
                    break
                continue
        if pending:
            operation_task.cancel()
            termination = await force_task_terminal(
                operation_task,
                timeout=self._cleanup_timeout,
            )
            for termination_error in termination.errors:
                self.disposal_errors.append((stage, termination_error))
            self.disposal_errors.append(
                (
                    stage,
                    TimeoutError(f"teardown operation exceeded {self._cleanup_timeout}s"),
                )
            )
            return
        try:
            operation_task.result()
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))

    async def _dispose(self, env: Environment) -> None:
        # Revoke the adapter synchronously before any teardown await.  A late
        # factory cannot hand a still-active environment to another consumer.
        env._aborted = True
        await self._finish_teardown_operation("environment abort failed", env.abort)
        await self._finish_teardown_operation(
            "environment cleanup failed",
            env.cleanup,
        )

    async def _run(self) -> None:
        try:
            env = await self._factory(self._eval_task)
        except asyncio.CancelledError:
            # A factory that propagates cancellation created no transferable
            # environment.  Factories that consume it continue below and their
            # eventual result remains owned here.
            return
        except BaseException as exc:
            if not self._delivery.done():
                self._delivery.set_exception(exc)
            return

        if not isinstance(env, Environment):
            if not self._delivery.done():
                self._delivery.set_exception(TypeError("env_factory must return an Environment instance"))
            return

        self._environment = env
        if self._delivery.done():
            self._dispose_requested = True
        else:
            self._delivery.set_result(env)

        while not self._transferred and not self._dispose_requested:
            try:
                await self._decision.wait()
            except asyncio.CancelledError:
                self._dispose_requested = True

        if self._transferred and not self._dispose_requested:
            return
        await self._dispose(env)


async def _stop_checkpoint_bounded(
    checkpoint: WorktreeCheckpoint,
    env: Environment,
    *,
    exclude_paths: Sequence[str],
    cleanup_timeout: float,
) -> tuple[bool, Any | None, tuple[asyncio.Task[Any], ...]]:
    stop_task = asyncio.ensure_future(checkpoint.stop(env, exclude_paths=exclude_paths))
    quiesced = await _wait_for_owned_execution(
        [stop_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if stop_task.cancelled():
            return False, None, ()
        return True, stop_task.result(), ()
    stop_task.add_done_callback(_consume_background_task)
    return False, None, (stop_task,)


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _workflow_persistence_errors(ctx: WorkflowContext) -> tuple[Exception, ...]:
    errors: list[Exception] = []
    for session in ctx.sessions:
        for error in getattr(session, "persistence_errors", ()):
            if isinstance(error, Exception):
                errors.append(error)
    return tuple(errors)


async def _finalize_eval_workflow_sessions(
    ctx: WorkflowContext,
    *,
    cleanup_timeout: float,
) -> tuple[bool, tuple[Exception, ...], tuple[asyncio.Task[Any], ...]]:
    """Freeze final session states after execution and await their writers."""
    enqueue_errors: list[Exception] = []
    for session in ctx.sessions:
        enqueue = getattr(session, "enqueue_auto_save", None)
        if not callable(enqueue):
            continue
        try:
            enqueue()
        except Exception as exc:
            enqueue_errors.append(exc)
    quiesced = await _wait_for_owned_execution(
        [],
        ctx,
        cleanup_timeout=cleanup_timeout,
    )
    pending = tuple(task for task in ctx.pending_cleanup_tasks if isinstance(task, asyncio.Task) and not task.done())
    return (
        quiesced,
        (*enqueue_errors, *_workflow_persistence_errors(ctx)),
        pending,
    )


def _eval_manifest_payload(
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
) -> dict[str, Any]:
    """Freeze values owned by the event-loop before file I/O starts."""
    return {
        "workflow": getattr(workflow, "__name__", "workflow"),
        "task_id": task.task_id,
        "sessions": len(ctx.sessions),
        "tokens_spent": ctx.budget.spent(),
        "budget_total": ctx.budget.total,
    }


def _write_eval_workflow_manifest(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Write ``<run_dir>/workflow.json`` summarising an eval workflow run."""
    if manifest is None:
        manifest = _eval_manifest_payload(
            task=task,
            workflow=workflow,
            ctx=ctx,
        )
    SessionStore().save_manifest(os.path.join(run_dir, WORKFLOW_MANIFEST_FILENAME), manifest)


def _eval_manifest_owner_done(task: asyncio.Task[Any]) -> None:
    _EVAL_MANIFEST_OWNER_TASKS.discard(task)
    _consume_background_task(task)


async def _await_eval_manifest_daemon_write(write: Any) -> None:
    await asyncio.wrap_future(write)


def _track_eval_manifest_daemon_writes(
    subscriber: AutoSaveSubscriber,
) -> set[asyncio.Task[Any]]:
    owners: set[asyncio.Task[Any]] = set()
    for write in subscriber.pending_write_futures:
        owner = asyncio.create_task(_await_eval_manifest_daemon_write(write))
        _EVAL_MANIFEST_OWNER_TASKS.add(owner)
        owner.add_done_callback(_eval_manifest_owner_done)
        owners.add(owner)
    return owners


async def _persist_eval_workflow_manifest_owned(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
    cleanup_timeout: float,
) -> tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]:
    manifest = _eval_manifest_payload(task=task, workflow=workflow, ctx=ctx)
    subscriber = AutoSaveSubscriber(
        lambda: _evaluator_module()._write_eval_workflow_manifest(
            run_dir,
            task=task,
            workflow=workflow,
            ctx=ctx,
            manifest=manifest,
        ),
        serialization_key=os.path.join(
            run_dir,
            WORKFLOW_MANIFEST_FILENAME,
        ),
    )
    owner = subscriber.enqueue()
    if owner is None:
        return True, subscriber.last_error, ()
    _EVAL_MANIFEST_OWNER_TASKS.add(owner)
    owner.add_done_callback(_eval_manifest_owner_done)
    pending: set[asyncio.Task[Any]] = {owner}
    _done, pending = await asyncio.wait(pending, timeout=cleanup_timeout)
    if pending:
        for pending_task in pending:
            pending_task.cancel()
        _done, pending = await asyncio.wait(pending, timeout=cleanup_timeout)
    if pending:
        await isolate_tasks_from_shutdown(pending, timeout=cleanup_timeout)
    write_owners = _track_eval_manifest_daemon_writes(subscriber)
    if write_owners:
        _done, write_owners = await asyncio.wait(write_owners, timeout=0)
        pending.update(write_owners)
    return not pending, subscriber.last_error, tuple(pending)


async def _cleanup_eval_resources_after_tasks(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Tracer,
    timeout: float,
    env: Environment | None = None,
) -> None:
    pending = {task for task in dependencies if not task.done()}
    deadline = asyncio.get_running_loop().time() + timeout
    while pending:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        waiter = asyncio.create_task(asyncio.wait(pending, timeout=remaining))
        while True:
            try:
                await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    break
                continue
        _done, pending = waiter.result()
    if pending:
        await isolate_tasks_from_shutdown(pending, timeout=timeout)
        _LATE_EVAL_RESOURCE_FAILURES.append(
            TimeoutError(
                "late evaluator resource dependencies did not quiesce; "
                "their environment and tracer remain retained"
            )
        )
        return
    if env is not None:
        try:
            cleaned = await _cleanup_environment_bounded(
                env,
                cleanup_timeout=timeout,
            )
        except BaseException as exc:
            _LATE_EVAL_RESOURCE_FAILURES.append(exc)
        else:
            if not cleaned:
                _LATE_EVAL_RESOURCE_FAILURES.append(
                    TimeoutError(
                        "late evaluator environment cleanup did not quiesce"
                    )
                )
    try:
        tracer.close()
    except BaseException as exc:
        _LATE_EVAL_RESOURCE_FAILURES.append(exc)
        raise


def _late_eval_resource_done(task: asyncio.Task[Any]) -> None:
    _LATE_EVAL_RESOURCE_TASKS.discard(task)
    _consume_background_task(task)


def _defer_eval_resource_cleanup(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Tracer,
    env: Environment | None,
    timeout: float,
) -> None:
    late_timeout = min(2.0, max(0.1, timeout))
    owner = asyncio.create_task(
        _cleanup_eval_resources_after_tasks(
            dependencies,
            tracer=tracer,
            env=env,
            timeout=late_timeout,
        )
    )
    _LATE_EVAL_RESOURCE_TASKS.add(owner)
    owner.add_done_callback(_late_eval_resource_done)
