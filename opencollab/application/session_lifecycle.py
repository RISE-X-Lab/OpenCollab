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


def _completed_session_output_before(session: Any, deadline: float | None) -> str | None:
    completed = getattr(session, "_completed_turn_output", None)
    if deadline is None or completed is None:
        return None
    finished_at, output = completed
    return output if finished_at < deadline else None


async def close_session_resources(
    sessions: Iterable[Any],
    *,
    timeout: float,
) -> bool:
    """Close each unique session and report whether every close succeeded."""
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
    _done, pending = await asyncio.wait(close_tasks, timeout=timeout)
    if pending:
        pending = await cancel_tasks_and_wait(pending, timeout=timeout)
    for task in close_tasks - pending:
        try:
            task.result()
        except BaseException:
            succeeded = False
    for task in pending:
        task.add_done_callback(consume_task_result)
    return succeeded and not pending

__all__ = ["close_session_resources"]
