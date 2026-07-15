"""Scheduler public turn and quiescence loop."""

from __future__ import annotations

import asyncio
import logging

from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)


class SchedulerRunMixin:
    async def run(self, user_message: str) -> str:
        """Send message to lead and run until the whole team is quiescent.

        A session that suspends on deferred work (``AWAITING_EVENTS``) returns
        its task while its children run; a child's completion re-activates it
        with a fresh task. So "all tasks done" is no longer the end — the team
        is finished only when no task is running, no pending table is
        outstanding, and every session is terminal or idle (``_quiescent``).
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks.add(current_task)
        try:
            async with self._run_lock:
                return await self._run_exclusive(user_message)
        except asyncio.CancelledError:
            # The public caller owns the whole team turn. Do not leave its lead
            # driver and descendants running after that owner is cancelled.
            if not self._shutting_down:
                if current_task is not None:
                    self._active_run_tasks.discard(current_task)
                await self.cleanup()
            raise
        finally:
            if current_task is not None:
                self._active_run_tasks.discard(current_task)

    async def _run_exclusive(self, user_message: str) -> str:
        """Drive one externally visible lead turn under ``_run_lock``."""
        if self._lead_session is None:
            raise RuntimeError("Scheduler has no lead session. Call create_init_process() first.")
        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")

        # A durable restore may reopen a prior turn with completed failure rows
        # for children that cannot survive process restart. Finish that turn
        # before appending a new user message, preserving tool-call ordering.
        if self._lead_session.state.phase is SessionPhase.AWAITING_EVENTS:
            task = self._tasks.get(0)
            if task is None or task.done():
                self._reserve_turn_budget(0)
                self._tasks[0] = asyncio.create_task(self._drive_agent(0, self._lead_session))
            await self.wait_until_terminal(0)

        # Restored teammate messages are scheduler-owned turns. Deliver and
        # finish them before accepting the new external user turn.
        if self._message_inbox.get(0):
            await self._drain_message_inbox(0)
            await self.wait_until_terminal(0)

        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        turn_start = len(self._lead_session.state.messages)
        prior_lease = self._current_turn_budget(0)
        self._reserve_turn_budget(0)
        await self._append_user_turn_txn(0, self._lead_session, user_message, prior_lease)
        if self._shutting_down:
            self._release_turn_budget(0)
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        self._tasks[0] = asyncio.create_task(self._drive_agent(0, self._lead_session))

        while True:
            for done_aid in [a for a, t in self._tasks.items() if t.done()]:
                task = self._tasks.pop(done_aid)
                try:
                    task.result()
                except asyncio.CancelledError:
                    logger.debug("background task for aid %s was cancelled", done_aid)
                except Exception as exc:
                    logger.error("background task for aid %s failed: %s", done_aid, exc)
            pending = list(self._tasks.values())
            if not pending:
                if self._quiescent():
                    break
                # All tasks drained but a wake's resume task may be mid-creation
                # (or a pending table is still open) — yield and re-check rather
                # than exit early. Non-empty pending tables keep us looping
                # without busy-spinning.
                await asyncio.sleep(0)
                continue
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        self._write_manifest()

        # Limit the answer lookup to messages appended during this invocation.
        # This keeps the public return value free of the worktree diff stored on
        # the SCB and prevents a precheck-only turn from leaking an old answer.
        for message in reversed(self._lead_session.state.messages[turn_start:]):
            if message.get("role") == "assistant" and message.get("content"):
                return message["content"]
        return ""

    def _quiescent(self) -> bool:
        """True when no session is mid-flight: none is awaiting events, none has
        an outstanding pending table, and every phase is terminal or idle.
        """
        for scb in self.table.entries.values():
            if self._message_inbox.get(scb.aid):
                return False
            if not scb.state.pending_events.is_empty():
                return False
            if not (scb.state.phase.is_terminal() or scb.state.phase is SessionPhase.IDLE):
                return False
        return True
