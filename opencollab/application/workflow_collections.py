"""Bounded ``parallel`` and ``pipeline`` primitives for workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

# A thunk is a zero-arg callable returning an awaitable result.
Thunk = Callable[[], Awaitable[Any]]

# A pipeline stage receives (previous result, original item, item index).
Stage = Callable[[Any, Any, int], Awaitable[Any]]


class WorkflowBudgetExceeded(Exception):
    """Raised when the shared budget is exhausted before an agent starts."""


@dataclass
class _TaskPermitState:
    """Shared state for one context-wide task-concurrency slot."""

    borrowers: int
    borrowers_done: asyncio.Event


@dataclass
class _TaskConcurrencyPermit:
    """A task slot view that may be re-entered or serially borrowed by children."""

    owner: asyncio.Task[Any] | None
    state: _TaskPermitState
    child_lock: asyncio.Lock
    closed: bool = False


class WorkflowCollectionsMixin:
    """Task-concurrency permits plus bounded collection orchestration."""

    async def _release_task_slot_after_borrowers(
        self,
        state: _TaskPermitState,
    ) -> None:
        await state.borrowers_done.wait()
        self._task_semaphore.release()

    async def _run_with_task_concurrency_permit(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one collection unit under a context-wide, task-reentrant slot."""
        current = asyncio.current_task()
        active = self._active_task_concurrency_permit.get()
        if active is not None and not active.closed:
            if active.owner is current:
                return await operation()

            # ContextVars are copied into child Tasks. Let one child at a time
            # borrow the parent's live slot, which keeps nested gather()
            # compositions deadlock-free at task_concurrency=1.
            async with active.child_lock:
                if not active.closed:
                    state = active.state
                    state.borrowers += 1
                    state.borrowers_done.clear()
                    borrowed = _TaskConcurrencyPermit(
                        owner=current,
                        state=state,
                        child_lock=asyncio.Lock(),
                    )
                    token = self._active_task_concurrency_permit.set(borrowed)
                    try:
                        return await operation()
                    finally:
                        borrowed.closed = True
                        self._active_task_concurrency_permit.reset(token)
                        state.borrowers -= 1
                        if state.borrowers == 0:
                            state.borrowers_done.set()

        await self._task_semaphore.acquire()
        borrowers_done = asyncio.Event()
        borrowers_done.set()
        state = _TaskPermitState(
            borrowers=0,
            borrowers_done=borrowers_done,
        )
        permit = _TaskConcurrencyPermit(
            owner=current,
            state=state,
            child_lock=asyncio.Lock(),
        )
        token = self._active_task_concurrency_permit.set(permit)
        try:
            return await operation()
        finally:
            permit.closed = True
            self._active_task_concurrency_permit.reset(token)
            if state.borrowers:
                release_task = asyncio.create_task(
                    self._release_task_slot_after_borrowers(state)
                )
                self._track_pending_cleanup(release_task)
            else:
                self._task_semaphore.release()

    async def _bounded_collection(
        self,
        size: int,
        run_unit: Callable[[int], Awaitable[Any]],
    ) -> list[Any]:
        """Run indexed units with O(task_concurrency) live asyncio tasks."""
        if size == 0:
            return []

        remaining = self.budget.remaining()
        if remaining == float("inf"):
            planned_budget = None
        else:
            available = max(0, int(remaining))
            planned_budget = max(1, available // size) if available else 0
        results: list[Any] = [None] * size
        next_index = iter(range(size))
        terminal: list[BaseException] = []

        async def execute(index: int) -> None:
            budget_token = self._active_collection_budget.set(planned_budget)
            try:
                results[index] = await self._run_with_task_concurrency_permit(
                    lambda: run_unit(index)
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                results[index] = exc
                terminal.append(exc)
            finally:
                self._active_collection_budget.reset(budget_token)

        async def worker() -> None:
            while not terminal:
                try:
                    index = next(next_index)
                except StopIteration:
                    return
                await execute(index)

        active = self._active_task_concurrency_permit.get()
        if (
            active is not None
            and not active.closed
            and active.owner is asyncio.current_task()
        ):
            while not terminal:
                try:
                    index = next(next_index)
                except StopIteration:
                    break
                await execute(index)
        else:
            workers = [
                asyncio.create_task(worker())
                for _ in range(min(size, self._task_concurrency))
            ]
            await asyncio.gather(*workers)

        for result in results:
            if isinstance(result, WorkflowBudgetExceeded):
                raise result
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def parallel(self, thunks: Sequence[Thunk]) -> list[Any]:
        """Run thunks concurrently behind the task-worker limit."""

        async def guard(thunk: Thunk) -> Any:
            try:
                return await thunk()
            except WorkflowBudgetExceeded:
                raise
            except Exception:
                return None

        return await self._bounded_collection(
            len(thunks),
            lambda index: guard(thunks[index]),
        )

    async def pipeline(
        self,
        items: Sequence[Any],
        *stages: Stage,
        stop_on_none: bool = True,
    ) -> list[Any]:
        """Flow each item through ordered stages under the task-worker limit."""

        async def flow(item: Any, idx: int) -> Any:
            result: Any = item
            for stage in stages:
                try:
                    result = await stage(result, item, idx)
                    if result is None and stop_on_none:
                        return None
                except WorkflowBudgetExceeded:
                    raise
                except Exception:
                    return None
            return result

        return await self._bounded_collection(
            len(items),
            lambda index: flow(items[index], index),
        )

