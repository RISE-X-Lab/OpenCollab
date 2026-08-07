from __future__ import annotations

import asyncio
import math
import warnings
from collections.abc import Awaitable, Iterable
from typing import Callable, TypeVar

from opencollab.application.exception_notes import add_exception_note

T = TypeVar("T")


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


async def force_task_terminal(
    task: asyncio.Future[object],
    *,
    timeout: float = 0.1,
) -> bool:
    """Cancel an async owner and wait up to one cooperative deadline.

    Returns whether the task reached a terminal state within the deadline; a
    still-running task means the deadline expired first. Callers only need that
    verdict, so the retrieved result/exception is consumed and discarded here.
    """
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

    async def wait_one_phase() -> None:
        deadline = asyncio.get_running_loop().time() + phase_timeout
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # Keep the owner alive through repeated caller cancellation.
                pass

    if not task.done():
        task.cancel()
        await wait_one_phase()
    if task.done():
        # Retrieve so asyncio doesn't warn the result was never consumed.
        try:
            task.result()
        except BaseException:
            pass
    return task.done()


async def cancel_tasks_and_wait(
    tasks: Iterable[asyncio.Future[object]],
    *,
    timeout: float,
) -> set[asyncio.Future[object]]:
    """Cancel unique pending tasks, wait once, and return any still pending."""
    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    return pending


async def abandon_on_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    *,
    timeout_error_type: type[CallerTimeoutError] = CallerTimeoutError,
    task_tracker: Callable[[asyncio.Task[T]], None] | None = None,
    late_result_handler: Callable[[asyncio.Future[T]], None] | None = None,
) -> T:
    """Await ``awaitable`` with a hard caller-side timeout.

    ``asyncio.wait_for`` waits for cancellation cleanup after the timeout fires.
    Some provider coroutines can spend a long time in that cleanup path, which
    keeps the caller stuck. This helper cancels the task at the deadline, lets an
    optional handler account for a delayed result in the background, and
    immediately raises TimeoutError to the caller.
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
        task.add_done_callback(consume_task_result)
        raise

    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(late_result_handler or consume_task_result)
    raise timeout_error_type


def consume_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except BaseException:
        pass


def run_with_bounded_shutdown(
    awaitable: Awaitable[T],
    *,
    shutdown_timeout: float = 2.0,
) -> T:
    """Run one CLI coroutine and give pending tasks a bounded cleanup phase."""
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
    result: T | None = None
    run_error: BaseException | None = None
    try:
        try:
            result = loop.run_until_complete(awaitable)
        except BaseException as exc:
            run_error = exc
        deadline = loop.time() + timeout
        observed: set[asyncio.Task[object]] = set()
        saw_empty = False
        while True:
            pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
            observed.update(pending)
            if not pending:
                if saw_empty or loop.time() >= deadline:
                    break
                saw_empty = True
                loop.run_until_complete(asyncio.sleep(0))
                continue
            saw_empty = False
            for task in pending:
                task.cancel()
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            loop.run_until_complete(asyncio.wait(pending, timeout=remaining))
        lingering = {task for task in asyncio.all_tasks(loop) if not task.done()}
        for task in observed:
            consume_task_result(task)
        if run_error is not None:
            raise run_error
        if lingering:
            # A background task that missed the shutdown deadline must not
            # discard the completed run's outcome. Surface it as a non-fatal
            # diagnostic instead of turning a successful run into a
            # crash-on-exit that loses the result.
            warnings.warn(
                f"{len(lingering)} async task(s) missed the shutdown deadline; "
                "the completed run's result was preserved",
                RuntimeWarning,
                stacklevel=2,
            )
        return result  # type: ignore[return-value]
    finally:
        loop.close()
        asyncio.set_event_loop(None)
