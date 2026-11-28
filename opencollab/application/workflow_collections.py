"""Bounded ``parallel`` and ``pipeline`` primitives for workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.application.async_timeout import await_owned_operation

Thunk = Callable[[], Awaitable[Any]]

# A pipeline stage receives (previous result, original item, item index).
Stage = Callable[[Any, Any, int], Awaitable[Any]]

class WorkflowBudgetExceeded(Exception):
    """Raised when the shared budget is exhausted before an agent starts."""

class WorkflowEnvironmentRevoked(RuntimeError):
    """The shared workflow environment can no longer execute agent work."""

@dataclass(eq=False)
class _CollectionBudget:
    size: int
    entry_balance: int
    parent: _CollectionBudget | None
    recovered_share: int = 0

    @property
    def entry_share(self) -> int:
        balance = self.entry_balance
        if self.parent is not None:
            balance = min(balance, self.parent.entry_share)
        return max(1, balance // self.size)

    def includes(self, member: _CollectionBudget | None) -> bool:
        while member is not None:
            if member is self:
                return True
            member = member.parent
        return False

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
    child_slot: asyncio.Semaphore
    closed: bool = False

class WorkflowCollectionsMixin:
    """Task-concurrency permits plus bounded collection orchestration."""

    async def _release_task_slot_after_borrowers(
        self,
        state: _TaskPermitState,
    ) -> None:
        await state.borrowers_done.wait()
        self._task_semaphore.release()

    async def _acquire_child_task_slot(self, inherited: asyncio.Semaphore) -> asyncio.Semaphore:
        shared = self._task_semaphore
        if self._task_concurrency == 1 or not inherited.locked():
            await inherited.acquire()
            return inherited
        if not shared.locked():
            await shared.acquire()
            return shared

        requests = {asyncio.create_task(slot.acquire()): slot for slot in (inherited, shared)}
        selected = None

        async def release_unused() -> None:
            unused = [task for task in requests if task is not selected]
            for task in unused:
                task.cancel()
            await asyncio.gather(*unused, return_exceptions=True)
            for task in unused:
                if not task.cancelled() and task.exception() is None:
                    requests[task].release()

        try:
            done, _ = await asyncio.wait(requests, return_when=asyncio.FIRST_COMPLETED)
            winner = next(task for task in requests if task in done)
            winner.result()
            selected = winner
            return requests[winner]
        finally:
            # Both reservations can succeed before the waiting task is scheduled.
            try:
                await await_owned_operation(release_unused(), propagate_cancellation=True)
            except BaseException:
                if selected is not None:
                    requests[selected].release()
                raise
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

            # Child tasks may borrow a parent's slot or use spare global capacity.
            slot = await self._acquire_child_task_slot(active.child_slot)
            if slot is self._task_semaphore:
                # Shared-slot work can outlive the parent that launched it.
                with self._own_workflow_call():
                    return await self._run_with_acquired_task_slot(operation)
            try:
                if not active.closed:
                    state = active.state
                    state.borrowers += 1
                    state.borrowers_done.clear()
                    borrowed = _TaskConcurrencyPermit(
                        owner=current,
                        state=state,
                        child_slot=asyncio.Semaphore(1),
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
            finally:
                slot.release()

        await self._task_semaphore.acquire()
        return await self._run_with_acquired_task_slot(operation)

    async def _run_with_acquired_task_slot(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Own an already acquired slot through execution and borrower cleanup."""
        current = asyncio.current_task()
        borrowers_done = asyncio.Event()
        borrowers_done.set()
        state = _TaskPermitState(
            borrowers=0,
            borrowers_done=borrowers_done,
        )
        permit = _TaskConcurrencyPermit(
            owner=current,
            state=state,
            child_slot=asyncio.Semaphore(1),
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

    def _start_acquired_task_worker(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> asyncio.Task[Any]:
        """Transfer a reserved slot to a worker, including pre-start cancellation."""
        started = False

        async def run() -> Any:
            nonlocal started
            started = True
            with self._own_workflow_call():
                return await self._run_with_acquired_task_slot(operation)

        def release_unstarted(_task: asyncio.Task[Any]) -> None:
            if not started:
                self._task_semaphore.release()

        task = asyncio.create_task(run())
        task.add_done_callback(release_unstarted)
        return task

    async def _bounded_collection(
        self,
        size: int,
        run_unit: Callable[[int], Awaitable[Any]],
    ) -> list[Any]:
        """Run indexed units with O(task_concurrency) live asyncio tasks."""
        if size == 0:
            return []

        starting_balance = self.budget.remaining()
        available_at_entry = int(starting_balance) if 0 < starting_balance < float("inf") else 0
        collection_budget = _CollectionBudget(size, available_at_entry, self._active_collection_budget.get())

        results: list[Any] = [None] * size
        next_index = iter(range(size))
        errors: dict[int, BaseException] = {}

        async def execute(index: int) -> None:
            budget_token = self._active_collection_budget.set(collection_budget)
            try:
                results[index] = await self._run_with_task_concurrency_permit(
                    lambda: run_unit(index)
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                errors[index] = exc
            finally:
                self._active_collection_budget.reset(budget_token)

        async def worker() -> None:
            while not errors:
                try:
                    index = next(next_index)
                except StopIteration:
                    return
                await execute(index)

        active = self._active_task_concurrency_permit.get()
        if active is not None and not active.closed:
            # Keep one worker on the parent's live slot. A single allocator can
            # admit extra workers as siblings release slots, without creating
            # a waiting worker for every nested item.
            workers = []
            extra_count = min(size - 1, self._task_concurrency - 1)
            cancelled_worker = asyncio.get_running_loop().create_future()

            def observe_worker_cancellation(task: asyncio.Task[Any]) -> None:
                if task.cancelled() and not cancelled_worker.done():
                    cancelled_worker.set_result(task)

            async def allocate_workers() -> None:
                for _ in range(extra_count):
                    await self._task_semaphore.acquire()
                    task = self._start_acquired_task_worker(worker)
                    workers.append(task)
                    task.add_done_callback(observe_worker_cancellation)

            if extra_count == 0:
                await worker()
            else:
                # Borrow the parent slot in a child task so another worker's
                # cancellation can interrupt it before its operation finishes.
                inline_worker = asyncio.create_task(worker())
                allocator = asyncio.create_task(allocate_workers())
                try:
                    done, _pending = await asyncio.wait(
                        {inline_worker, cancelled_worker},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancelled_worker in done:
                        cancelled = cancelled_worker.result()
                        cancelled.result()
                    await inline_worker
                    allocator.cancel()
                    await asyncio.gather(allocator, return_exceptions=True)
                    await asyncio.gather(*workers)
                except BaseException:
                    inline_worker.cancel()
                    allocator.cancel()
                    for task in workers:
                        task.cancel()
                    await await_owned_operation(
                        asyncio.gather(inline_worker, allocator, *workers, return_exceptions=True),
                    )
                    raise
        else:
            workers = [
                asyncio.create_task(worker())
                for _ in range(min(size, self._task_concurrency))
            ]
            group = asyncio.gather(*workers)
            try:
                await asyncio.shield(group)
            except BaseException:
                for task in workers:
                    task.cancel()
                await await_owned_operation(
                    asyncio.gather(group, *workers, return_exceptions=True),
                )
                raise

        for index in sorted(errors):
            if isinstance(errors[index], WorkflowBudgetExceeded):
                raise errors[index]
        if errors:
            raise errors[min(errors)]
        return results

    async def parallel(self, thunks: Sequence[Thunk]) -> list[Any]:
        """Run thunks concurrently behind the task-worker limit."""

        async def guard(thunk: Thunk) -> Any:
            try:
                return await thunk()
            except (WorkflowBudgetExceeded, WorkflowEnvironmentRevoked):
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
                except (WorkflowBudgetExceeded, WorkflowEnvironmentRevoked):
                    raise
                except Exception:
                    return None
            return result

        return await self._bounded_collection(
            len(items),
            lambda index: flow(items[index], index),
        )
