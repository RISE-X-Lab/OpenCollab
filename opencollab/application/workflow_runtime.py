"""Timeout and cleanup runtime primitives for :class:`WorkflowContext`.

The workflow engine keeps orchestration policy in ``workflow.py`` while this
small mixin owns the process-local lifecycle of calls, leases, semaphore
permits, and deadline-bound tasks.  Keeping those mechanics together makes
the cancellation contract auditable without changing the public context API.

Pure application layer: domain + stdlib imports only.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from opencollab.application.async_timeout import (
    CallerTimeoutError,
    consume_task_result,
)
from opencollab.application.workflow_budget import _ConcurrencyPermit

DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS = 120.0


class WorkflowRuntimeMixin:
    """Call cleanup, concurrency permits, and timeout helpers."""

    async def _release_call_after_tasks(
        self,
        lease: Any,
        tasks: list[asyncio.Task[Any]],
        *,
        release_slot: bool,
    ) -> None:
        """Release one timed-out call's budget and slot after it is quiescent."""
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.budget.release(lease)
            if release_slot:
                self._semaphore.release()

    def _release_lease_when_quiescent(
        self,
        lease: Any,
        *,
        release_slot: bool,
    ) -> bool:
        """Release now, or hand the lease and semaphore slot to cleanup."""
        pending = [task for task in (lease.pending_tasks or []) if not task.done()]
        if not pending:
            self.budget.release(lease)
            return False
        cleanup_task = asyncio.create_task(
            self._release_call_after_tasks(
                lease,
                pending,
                release_slot=release_slot,
            )
        )
        self._track_pending_cleanup(cleanup_task)
        if not release_slot:
            permit = self._active_concurrency_permit.get()
            if permit is not None and permit.owner is asyncio.current_task():
                permit.pending_cleanup_tasks.append(cleanup_task)
        return release_slot

    async def _release_slot_after_tasks(
        self,
        tasks: list[asyncio.Task[Any]],
    ) -> None:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._semaphore.release()

    async def _run_with_concurrency_permit(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one collection unit under a task-reentrant shared permit."""
        current = asyncio.current_task()
        active = self._active_concurrency_permit.get()
        if active is not None and active.owner is current:
            return await operation()

        await self._semaphore.acquire()
        permit = _ConcurrencyPermit(owner=current, pending_cleanup_tasks=[])
        token = self._active_concurrency_permit.set(permit)
        handed_to_cleanup = False
        try:
            return await operation()
        finally:
            pending = [
                task for task in permit.pending_cleanup_tasks if not task.done()
            ]
            if pending:
                release_task = asyncio.create_task(
                    self._release_slot_after_tasks(pending)
                )
                self._track_pending_cleanup(release_task)
                handed_to_cleanup = True
            self._active_concurrency_permit.reset(token)
            if not handed_to_cleanup:
                self._semaphore.release()

    def _track_pending_cleanup(self, task: asyncio.Task[Any]) -> None:
        """Own a background cleanup task and always consume its final result."""
        if task.done():
            consume_task_result(task)
            return
        self._pending_cleanup_tasks.add(task)
        task.add_done_callback(self._pending_cleanup_done)

    def _pending_cleanup_done(self, task: asyncio.Task[Any]) -> None:
        self._pending_cleanup_tasks.discard(task)
        consume_task_result(task)

    def _active_session_done(self, task: asyncio.Task[Any]) -> None:
        self._active_session_tasks.discard(task)
        consume_task_result(task)

    @staticmethod
    def _normalize_timeout(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if isinstance(timeout, bool):
            raise ValueError("workflow timeout must be positive, finite, infinity, or None")
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "workflow timeout must be positive, finite, infinity, or None"
            ) from exc
        if math.isnan(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                "workflow timeout must be positive, finite, infinity, or None"
            )
        if math.isinf(timeout_seconds):
            return None
        return timeout_seconds

    async def _run_with_timeout(self, awaitable: Awaitable[Any], timeout: float | None) -> Any:
        timeout_seconds = self._normalize_timeout(timeout)
        task = asyncio.ensure_future(awaitable)
        self._active_session_tasks.add(task)
        task.add_done_callback(self._active_session_done)
        try:
            if timeout_seconds is None:
                return await asyncio.shield(task)
            done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
            if task in done:
                return task.result()
            task.cancel()
            raise CallerTimeoutError
        except (CallerTimeoutError, asyncio.CancelledError):
            if not task.done():
                task.cancel()
            lease = self._active_budget_lease.get()
            if not task.done():
                self._track_pending_cleanup(task)
            if lease is not None and not task.done():
                if lease.pending_tasks is None:
                    lease.pending_tasks = []
                lease.pending_tasks.append(task)
            raise

    @staticmethod
    def _timeout_deadline(timeout: float | None) -> float | None:
        timeout_seconds = WorkflowRuntimeMixin._normalize_timeout(timeout)
        if timeout_seconds is None:
            return None
        return time.monotonic() + timeout_seconds

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CallerTimeoutError
        return remaining
