"""Stable bounded-lifecycle primitives for integration-owned async work."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

from opencollab.adapters._env_process import _await_owned_operation
from opencollab.application.async_timeout import (
    CallerTimeoutError,
    TaskTerminationResult,
    abandon_on_timeout,
    force_task_terminal,
    isolate_tasks_from_shutdown,
    run_with_bounded_shutdown,
    task_is_isolated,
)
from opencollab.application.exception_notes import add_exception_note

from .environment import ExecutionEnvironment

T = TypeVar("T")


def revoke_environment(environment: ExecutionEnvironment) -> None:
    """Synchronously prevent an environment from accepting new side effects."""
    environment.revoke()


async def await_owned_operation(
    awaitable: Awaitable[T],
    *,
    propagate_cancellation: bool = False,
) -> T:
    """Await owned teardown through repeated caller cancellation."""
    return await _await_owned_operation(
        awaitable,
        propagate_cancellation=propagate_cancellation,
    )


__all__ = [
    "CallerTimeoutError",
    "TaskTerminationResult",
    "add_exception_note",
    "abandon_on_timeout",
    "await_owned_operation",
    "force_task_terminal",
    "isolate_tasks_from_shutdown",
    "revoke_environment",
    "run_with_bounded_shutdown",
    "task_is_isolated",
]
