"""Scheduler wait helpers and self-review orchestration."""

from __future__ import annotations

import asyncio

from opencollab.application.self_collaboration import run_spawn_with_review


class SchedulerReviewMixin:
    async def wait_for(self, aid: int) -> None:
        """Wait until ``aid``'s current driving task settles.

        Returns immediately when no task is tracked for ``aid``. A session
        that suspends on deferred work returns its task while its children
        run, so this waits for the current task only, not for the session to
        reach a terminal phase.
        """
        task = self._tasks.get(aid)
        if task is not None:
            await asyncio.shield(task)

    async def wait_until_terminal(self, aid: int) -> None:
        """Wait through every suspend/resume cycle until ``aid`` is terminal.

        A driving task can finish while the session is only awaiting a nested
        child. Follow replacement tasks installed by ``_wake`` so callers such
        as the review loop never inspect an intermediate SCB result.
        """
        while True:
            scb = self.table.get(aid)
            if scb is None:
                raise LookupError(f"Unknown agent id: {aid}")

            task = self._tasks.get(aid)
            if scb.state.phase.is_terminal():
                if task is not None and not task.done():
                    await asyncio.shield(task)
                    continue
                # A finishing task may have installed a message-driven
                # replacement after the terminal phase was first observed.
                if self._tasks.get(aid) is not task:
                    continue
                return

            if task is not None and not task.done():
                await asyncio.shield(task)
            else:
                await self._wait_for_scheduler_progress(aid)

    async def spawn_with_review(
        self,
        parent_aid: int,
        task: str,
        context: str = "",
        max_iterations: int = 3,
    ) -> str:
        """Self-Collaboration: Coder -> Reviewer loop (see ``self_collaboration``)."""
        tracker = {"outstanding": 0}
        token = self._review_parent_lease_tracker.set((parent_aid, tracker))
        try:
            return await run_spawn_with_review(self, parent_aid, task, context, max_iterations)
        finally:
            self._review_parent_lease_tracker.reset(token)
            if tracker["outstanding"] > 0 and not self._shutting_down and self.table.get(parent_aid) is not None:
                self._reserve_turn_lease(parent_aid)
