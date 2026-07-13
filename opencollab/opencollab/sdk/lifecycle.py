"""Stable bounded-lifecycle primitives for integration-owned async work."""

from __future__ import annotations

from opencollab.application.async_timeout import (
    CallerTimeoutError,
    abandon_on_timeout,
    await_owned_operation,
    force_task_terminal,
    isolate_tasks_from_shutdown,
    run_with_bounded_shutdown,
)
from opencollab.application.exception_notes import add_exception_note

__all__ = [
    "CallerTimeoutError",
    "add_exception_note",
    "abandon_on_timeout",
    "await_owned_operation",
    "force_task_terminal",
    "isolate_tasks_from_shutdown",
    "run_with_bounded_shutdown",
]
