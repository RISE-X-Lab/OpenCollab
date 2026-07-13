from __future__ import annotations

import asyncio
import math
import queue
import threading
import weakref
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Callable, TypeVar

from opencollab.application.exception_notes import add_exception_note

T = TypeVar("T")
_MAX_RETAINED_DETACHED_TASKS = 256
_DETACHED_TASK_QUEUE: queue.SimpleQueue[
    tuple[str, asyncio.Future[object]]
] = queue.SimpleQueue()
_DETACHED_TASK_KEEPER_STARTED = False
_DETACHED_TASK_KEEPER_LOCK = threading.Lock()
_DETACHED_TASK_COUNT = 0
_ASYNC_RUNTIME_UNHEALTHY = False
_ISOLATED_TASKS: weakref.WeakSet[asyncio.Future[object]] = weakref.WeakSet()


async def await_owned_operation(
    awaitable: Awaitable[T],
    *,
    propagate_cancellation: bool = False,
) -> T:
    """Keep a bounded owner alive through repeated caller cancellation."""
    owner = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(owner)
            break
        except asyncio.CancelledError as exc:
            if owner.done() and owner.cancelled():
                raise
            if cancellation is None:
                cancellation = exc
        except BaseException as exc:
            if cancellation is not None and propagate_cancellation:
                add_exception_note(
                    cancellation,
                    f"owned operation also failed: {type(exc).__name__}: {exc}"
                )
                raise cancellation
            raise
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return result


class CallerTimeoutError(asyncio.TimeoutError):
    """Raised only when this helper's caller-supplied deadline expires.

    A provider or session may independently raise ``asyncio.TimeoutError``.
    Keeping the caller deadline distinct lets orchestration report transport
    failures accurately instead of labelling every timeout as a workflow wall.
    """


class AsyncRuntimeUnhealthyError(RuntimeError):
    """Raised when detached-owner capacity can no longer guarantee isolation."""


@dataclass(frozen=True)
class TaskTerminationResult:
    terminal: bool
    isolated: bool
    cancellation: asyncio.CancelledError | None
    errors: tuple[BaseException, ...]


async def force_task_terminal(
    task: asyncio.Future[object],
    *,
    timeout: float = 0.1,
    cancellation: asyncio.CancelledError | None = None,
) -> TaskTerminationResult:
    """Cancel an async owner, then isolate it from shutdown if it resists."""
    global _ASYNC_RUNTIME_UNHEALTHY
    if _ASYNC_RUNTIME_UNHEALTHY:
        raise AsyncRuntimeUnhealthyError(
            "async runtime is unhealthy; process restart required"
        )
    if isinstance(timeout, bool):
        raise ValueError("task termination timeout must be finite and positive")
    try:
        phase_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "task termination timeout must be finite and positive"
        ) from exc
    if not math.isfinite(phase_timeout) or phase_timeout <= 0:
        raise ValueError("task termination timeout must be finite and positive")
    if task is asyncio.current_task():
        raise RuntimeError("cannot force the current task to terminate")
    if task_is_isolated(task):
        return TaskTerminationResult(
            terminal=task.done(),
            isolated=not task.done(),
            cancellation=cancellation,
            errors=(),
        )

    errors: list[BaseException] = []

    async def wait_one_phase() -> None:
        nonlocal cancellation
        deadline = asyncio.get_running_loop().time() + phase_timeout
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

    if not task.done():
        task.cancel()
        await wait_one_phase()
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            errors.append(exc)
    else:
        over_capacity = _detach_task_from_loop(task)
        errors.append(
            TimeoutError(
                "async owner was isolated after it did not reach a terminal state"
            )
        )
        if over_capacity:
            _ASYNC_RUNTIME_UNHEALTHY = True
            raise AsyncRuntimeUnhealthyError(
                "detached async owner capacity exceeded; process restart required"
            )
    return TaskTerminationResult(
        terminal=task.done(),
        isolated=not task.done(),
        cancellation=cancellation,
        errors=tuple(errors),
    )


def task_is_isolated(task: asyncio.Future[object]) -> bool:
    return task in _ISOLATED_TASKS


async def isolate_tasks_from_shutdown(
    tasks: Iterable[asyncio.Future[object]],
    *,
    timeout: float,
) -> tuple[TaskTerminationResult, ...]:
    unique = tuple(dict.fromkeys(task for task in tasks if not task.done()))
    if not unique:
        return ()
    results = await asyncio.gather(
        *(force_task_terminal(task, timeout=timeout) for task in unique)
    )
    return tuple(results)


def _detach_task_from_loop(task: asyncio.Future[object]) -> bool:
    """Remove a revoked task from loop shutdown and retain it in a daemon owner."""
    global _DETACHED_TASK_COUNT, _DETACHED_TASK_KEEPER_STARTED
    task._log_destroy_pending = False  # type: ignore[attr-defined]
    _ISOLATED_TASKS.add(task)
    unregistered = False
    try:
        import _asyncio

        unregister = getattr(_asyncio, "_unregister_task", None)
        if callable(unregister):
            unregister(task)
            unregistered = True
        unregister_eager = getattr(_asyncio, "_unregister_eager_task", None)
        if callable(unregister_eager):
            try:
                unregister_eager(task)
            except (KeyError, RuntimeError):
                pass
    except (ImportError, RuntimeError):
        pass
    if not unregistered:
        unregister = getattr(asyncio.tasks, "_unregister_task", None)
        if callable(unregister):
            unregister(task)
            unregistered = True
    if not unregistered:
        registry = getattr(asyncio.tasks, "_all_tasks", None)
        discard = getattr(registry, "discard", None)
        if callable(discard):
            discard(task)
            unregistered = True

    with _DETACHED_TASK_KEEPER_LOCK:
        if not _DETACHED_TASK_KEEPER_STARTED:
            def keep_detached_tasks() -> None:
                retained: dict[int, asyncio.Future[object]] = {}
                while True:
                    action, retained_task = _DETACHED_TASK_QUEUE.get()
                    if action == "add":
                        retained[id(retained_task)] = retained_task
                    else:
                        retained.pop(id(retained_task), None)

            threading.Thread(
                target=keep_detached_tasks,
                name="opencollab-detached-task-owner",
                daemon=True,
            ).start()
            _DETACHED_TASK_KEEPER_STARTED = True
        _DETACHED_TASK_COUNT += 1
        over_capacity = _DETACHED_TASK_COUNT > _MAX_RETAINED_DETACHED_TASKS

    def release_if_done(done: asyncio.Future[object]) -> None:
        global _DETACHED_TASK_COUNT
        _DETACHED_TASK_QUEUE.put(("remove", done))
        with _DETACHED_TASK_KEEPER_LOCK:
            _DETACHED_TASK_COUNT = max(0, _DETACHED_TASK_COUNT - 1)

    task.add_done_callback(release_if_done)
    _DETACHED_TASK_QUEUE.put(("add", task))
    if not unregistered:
        over_capacity = True
    return over_capacity


async def abandon_on_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    *,
    timeout_error_type: type[CallerTimeoutError] = CallerTimeoutError,
    task_tracker: Callable[[asyncio.Task[T]], None] | None = None,
) -> T:
    """Await ``awaitable`` with a hard caller-side timeout.

    ``asyncio.wait_for`` waits for cancellation cleanup after the timeout fires.
    Some provider coroutines can spend a long time in that cleanup path, which
    keeps the caller stuck. This helper cancels the task at the deadline, drains
    its eventual result in the background, and immediately raises TimeoutError to
    the caller.
    """
    if timeout is None:
        return await awaitable
    if isinstance(timeout, bool):
        raise ValueError("timeout must be a finite positive number or None")
    try:
        normalized_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite positive number or None") from exc
    if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
        raise ValueError("timeout must be a finite positive number or None")

    task = asyncio.ensure_future(awaitable)
    if task_tracker is not None and isinstance(task, asyncio.Task):
        task_tracker(task)
    try:
        done, _ = await asyncio.wait({task}, timeout=normalized_timeout)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise

    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_task_result)
    raise timeout_error_type


def _consume_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except BaseException:
        pass


def run_with_bounded_shutdown(
    awaitable: Awaitable[T],
    *,
    shutdown_timeout: float = 2.0,
) -> T:
    """Run one CLI coroutine without an unbounded pending-task shutdown."""
    if isinstance(shutdown_timeout, bool):
        raise ValueError("shutdown_timeout must be a finite positive number")
    try:
        timeout = float(shutdown_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("shutdown_timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("shutdown_timeout must be a finite positive number")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run_with_bounded_shutdown cannot run inside an event loop")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    unhealthy = False
    try:
        return loop.run_until_complete(awaitable)
    finally:
        pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
        for task in pending:
            task.cancel()
        for task in pending:
            unhealthy = _detach_task_from_loop(task) or unhealthy
        loop.close()
        asyncio.set_event_loop(None)
        if unhealthy:
            global _ASYNC_RUNTIME_UNHEALTHY
            _ASYNC_RUNTIME_UNHEALTHY = True
            raise AsyncRuntimeUnhealthyError(
                "bounded shutdown isolation capacity exceeded; process restart required"
            )
