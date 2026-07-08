from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def abandon_on_timeout(awaitable: Awaitable[T], timeout: float | None) -> T:
    """Await ``awaitable`` with a hard caller-side timeout.

    ``asyncio.wait_for`` waits for cancellation cleanup after the timeout fires.
    Some provider coroutines can spend a long time in that cleanup path, which
    keeps the caller stuck. This helper cancels the task at the deadline, drains
    its eventual result in the background, and immediately raises TimeoutError to
    the caller.
    """
    if timeout is None or timeout == float("inf") or timeout <= 0:
        return await awaitable

    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise

    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_task_result)
    raise asyncio.TimeoutError


def _consume_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except BaseException:
        pass
