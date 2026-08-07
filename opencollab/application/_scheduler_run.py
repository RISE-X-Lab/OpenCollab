"""Scheduler public turn and quiescence loop."""

from __future__ import annotations

import asyncio
import logging

from opencollab.application.scheduler_types import SchedulerStalledError, SchedulerTurnError
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)


class SchedulerRunMixin:
    async def run(self, user_message: str) -> str:
        """Compatibility entry point for an external turn addressed to Lead."""
        return await self.run_turn(0, user_message)

    async def run_turn(self, aid: int, user_message: str) -> str:
        """Send an external user turn to ``aid`` and wait for team quiescence.

        A session that suspends on deferred work (``AWAITING_EVENTS``) returns
        its task while its children run; a child's completion re-activates it
        with a fresh task. So "all tasks done" is no longer the end — the team
        is finished only when no task is running, no pending table is
        outstanding, and every session is terminal or idle (``_quiescent``).
        """
        if isinstance(aid, bool) or not isinstance(aid, int) or aid < 0:
            raise ValueError("Agent aid must be a non-negative integer.")
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks[current_task] = aid
        try:
            lock = self._run_locks.setdefault(aid, asyncio.Lock())
            async with lock:
                return await self._run_turn_exclusive(aid, user_message)
        except asyncio.CancelledError:
            # The public caller owns the whole team turn. Do not leave its target
            # driver and descendants running after that owner is cancelled.
            if not self._shutting_down:
                if current_task is not None:
                    if self._active_run_tasks.get(current_task) == aid:
                        self._active_run_tasks.pop(current_task, None)
                await self.cleanup()
            raise
        finally:
            if current_task is not None:
                if self._active_run_tasks.get(current_task) == aid:
                    self._active_run_tasks.pop(current_task, None)

    async def _run_turn_exclusive(self, aid: int, user_message: str) -> str:
        """Drive one externally visible agent turn under its per-aid lock."""
        session = self._sessions.get(aid)
        scb = self.table.get(aid)
        if session is None or scb is None:
            if aid == 0 and self._lead_session is None:
                raise RuntimeError(
                    "Scheduler has no lead session. Call create_init_process() first."
                )
            raise ValueError(f"Cannot run user turn: no agent with aid {aid}.")
        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")

        task = self._tasks.get(aid)
        if task is not None and not task.done():
            raise RuntimeError(f"Cannot run user turn for aid {aid}: agent is still running.")

        # A durable restore may reopen a prior turn with completed failure rows
        # for children that cannot survive process restart. Finish that turn
        # before appending a new user message, preserving tool-call ordering.
        if session.state.phase is SessionPhase.AWAITING_EVENTS:
            task = self._tasks.get(aid)
            if task is None or task.done():
                self._reserve_turn_lease(aid)
                self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))
            await self.wait_until_terminal(aid)

        # A snapshot can capture the durable gap after an external user message
        # was accepted but before its driver task began. Finish that queued turn
        # before accepting another external message, so restore never fuses two
        # unrelated user requests into one provider prompt.
        if getattr(session.state, "pending_external_user_turn", None) is not None:
            self._reserve_turn_lease(aid)
            self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))
            await self.wait_until_terminal(aid)

        # Restored teammate messages are scheduler-owned turns. Deliver and
        # finish them before accepting the new external user turn.
        if self._message_inbox.get(aid):
            await self._drain_message_inbox(aid)
            await self.wait_until_terminal(aid)

        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        turn_start = len(session.state.messages)
        prior_lease = self._current_turn_lease(aid)
        if aid == 0:
            self._reserve_turn_lease(aid)
        elif not self._reserve_message_budget(aid):
            raise RuntimeError(
                f"Cannot run user turn for aid {aid}: no token budget is available."
            )
        await self._append_user_turn_txn(aid, session, user_message, prior_lease)
        if self._shutting_down:
            self._release_turn_lease(aid)
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))

        while True:
            for done_aid in [a for a, t in self._tasks.items() if t.done()]:
                task = self._tasks.pop(done_aid, None)
                if task is None:
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    logger.debug("background task for aid %s was cancelled", done_aid)
                except Exception as exc:
                    logger.error("background task for aid %s failed: %s", done_aid, exc)
            pending = self._active_scheduler_tasks()
            if not pending:
                if self._quiescent():
                    break
                await self._wait_for_scheduler_progress(aid)
                continue
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        self._write_manifest()

        # Limit the answer lookup to messages appended during this invocation.
        # This keeps the public return value free of the worktree diff stored on
        # the SCB and prevents a precheck-only turn from leaking an old answer.
        partial_answer = ""
        for message in reversed(session.state.messages[turn_start:]):
            if message.get("role") == "assistant" and message.get("content"):
                partial_answer = message["content"]
                break
        phase = scb.state.phase
        if phase in {SessionPhase.ERROR, SessionPhase.STOPPED}:
            raise SchedulerTurnError(
                aid,
                phase,
                scb.state.terminal_reason,
                partial_answer or None,
            )
        return partial_answer

    def _active_scheduler_tasks(self) -> set[asyncio.Task]:
        """Return live scheduler-owned producers, excluding the current waiter."""
        current = asyncio.current_task()
        return {
            task
            for task in (
                *self._tasks.values(),
                *self._startup_tasks.values(),
                *self._message_delivery_tasks.values(),
            )
            if not task.done() and task is not current
        }

    async def _wait_for_scheduler_progress(self, aid: int) -> None:
        """Block for a producer completion or report a state with no producer."""
        producers = self._active_scheduler_tasks()
        if not producers:
            raise SchedulerStalledError(aid)
        await asyncio.wait(producers, return_when=asyncio.FIRST_COMPLETED)

    def _quiescent(self) -> bool:
        """True when no session is mid-flight: none is awaiting events, none has
        an outstanding pending table, and every phase is terminal or idle.
        """
        if any(
            not task.done()
            for task in (
                *self._tasks.values(),
                *self._startup_tasks.values(),
                *self._message_delivery_tasks.values(),
            )
        ):
            return False
        for scb in self.table.entries.values():
            if self._message_inbox.get(scb.aid):
                return False
            if not scb.state.pending_events.is_empty():
                return False
            if not (scb.state.phase.is_terminal() or scb.state.phase is SessionPhase.IDLE):
                return False
        return True
