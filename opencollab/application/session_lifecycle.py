"""Bounded teardown for resources owned by completed sessions."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from typing import Any

from opencollab.application.async_timeout import (
    cancel_tasks_and_wait,
    consume_task_result,
)


async def close_session_resources(
    sessions: Iterable[Any],
    *,
    timeout: float,
) -> bool:
    """Close each unique session and prove every async close has terminated."""
    close_tasks: set[asyncio.Task[Any]] = set()
    succeeded = True
    seen: set[int] = set()
    for session in sessions:
        if id(session) in seen:
            continue
        seen.add(id(session))
        close = getattr(session, "aclose", None)
        if not callable(close):
            continue
        try:
            outcome = close()
        except BaseException:
            succeeded = False
            continue
        if inspect.isawaitable(outcome):
            close_tasks.add(asyncio.ensure_future(outcome))

    if not close_tasks:
        return succeeded
    done, pending = await asyncio.wait(close_tasks, timeout=timeout)
    for task in done:
        try:
            task.result()
        except BaseException:
            succeeded = False
    if pending:
        pending = await cancel_tasks_and_wait(pending, timeout=timeout)
    for task in close_tasks - pending:
        consume_task_result(task)
    return succeeded and not pending


__all__ = ["close_session_resources"]
