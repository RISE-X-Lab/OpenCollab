"""Scheduler public turn and quiescence loop."""

from __future__ import annotations

import asyncio
import logging

from opencollab.application.scheduler_types import SchedulerStalledError, SchedulerTurnError
from opencollab.domain.pending import RowStatus
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)


class SchedulerRunMixin:
    async def run(
        self,
        user_message: str,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Compatibility entry point for an external turn addressed to Lead."""
        return await self.run_turn(0, user_message, cancel_event=cancel_event)

    async def run_turn(
        self,
        aid: int,
        user_message: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Send an external user turn to ``aid`` and wait for team quiescence.

        A session that suspends on deferred work (``AWAITING_EVENTS``) returns
        its task while its children run; a child's completion re-activates it
        with a fresh task. So "all tasks done" is no longer the end — the team
        is finished only when no task is running, no pending table is
        outstanding, and every session is terminal or idle (``_quiescent``).
        """
        if isinstance(aid, bool) or not isinstance(aid, int) or aid < 0:
            raise ValueError("Agent aid must be a non-negative integer.")
        if cancel_event is not None and not isinstance(cancel_event, asyncio.Event):
            raise TypeError("cancel_event must be an asyncio.Event or None.")
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks[current_task] = aid
        try:
            lock = self._run_locks.setdefault(aid, asyncio.Lock())
            async with lock:
                if cancel_event is not None:
                    self._turn_cancel_events[aid] = cancel_event
                try:
                    return await self._run_turn_exclusive(aid, user_message)
                finally:
                    if self._turn_cancel_events.get(aid) is cancel_event:
                        self._turn_cancel_events.pop(aid, None)
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
        # A prebuilt team must be seated before the first model call, so the
        # roster an assigned topology names is the roster that ran. No-op unless
        # the scheduler was constructed with ``prebuild_team``, and idempotent.
        await self.ensure_team_prebuilt()
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
                self._start_agent_task(aid, session)
            await self.wait_until_terminal(aid)

        # A snapshot can capture the durable gap after an external user message
        # was accepted but before its driver task began. Finish that queued turn
        # before accepting another external message, so restore never fuses two
        # unrelated user requests into one provider prompt.
        if getattr(session.state, "pending_external_user_turn", None) is not None:
            self._reserve_turn_lease(aid)
            self._start_agent_task(aid, session)
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
        if self._entry_agent_takes_the_pool(aid):
            self._reserve_turn_lease(aid)
        elif not self._reserve_message_budget(aid):
            raise RuntimeError(
                f"Cannot run user turn for aid {aid}: no token budget is available."
            )
        await self._append_user_turn_txn(aid, session, user_message, prior_lease)
        if self._shutting_down:
            self._release_turn_lease(aid)
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        self._start_agent_task(aid, session)

        cancel_event = self._turn_cancel_events.get(aid)
        cancel_waiter = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        cancellation_requested = bool(
            cancel_event is not None and cancel_event.is_set()
        )
        try:
            while True:
                for done_aid in [a for a, t in self._tasks.items() if t.done()]:
                    task = self._tasks.pop(done_aid, None)
                    if task is None:
                        continue
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        logger.debug(
                            "background task for aid %s was cancelled",
                            done_aid,
                        )
                    except Exception as exc:
                        logger.error(
                            "background task for aid %s failed: %s",
                            done_aid,
                            exc,
                        )
                if cancel_waiter is not None and cancel_waiter.done():
                    cancellation_requested = True
                    cancel_waiter = None
                if cancellation_requested:
                    await self._settle_cancelled_suspended_turn(aid)
                pending = self._active_scheduler_tasks()
                if not pending:
                    if self._quiescent():
                        break
                    await self._wait_for_scheduler_progress(aid)
                    continue
                # The waiter joins the wait, never the liveness question: it
                # never completes on its own, so counting it as pending work
                # would keep a quiescent team's turn running until someone
                # cancelled it.
                if cancel_waiter is not None:
                    pending.add(cancel_waiter)
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
                await asyncio.gather(cancel_waiter, return_exceptions=True)

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

    def _turn_descendant_aids(self, aid: int) -> set[int]:
        """Return descendants referenced by this turn's live pending graph."""
        descendants: set[int] = set()
        changed = True
        while changed:
            changed = False
            parents = {aid, *descendants}
            for parent_aid in parents:
                parent = self.table.get(parent_aid)
                if parent is None:
                    continue
                for row in parent.state.pending_events.rows.values():
                    child_aid = row.ref
                    if (
                        row.status is RowStatus.PENDING
                        and isinstance(child_aid, int)
                        and not isinstance(child_aid, bool)
                        and child_aid != aid
                        and child_aid not in descendants
                    ):
                        descendants.add(child_aid)
                        changed = True
            for child_aid, origin in self._spawn_origin.items():
                if child_aid not in descendants and origin[0] in parents:
                    descendants.add(child_aid)
                    changed = True
            for child_aid, origin in self._startup_origin.items():
                if child_aid not in descendants and origin[0] in parents:
                    descendants.add(child_aid)
                    changed = True
        return descendants

    async def _settle_cancelled_suspended_turn(self, aid: int) -> None:
        """Cancel one suspended turn subtree without shutting down the team."""
        target = self.table.get(aid)
        if target is None or target.state.phase is not SessionPhase.AWAITING_EVENTS:
            return

        reason = "interrupted by user"
        failure = f"Error: {reason}"
        descendants = self._turn_descendant_aids(aid)
        # Close every wake gate in the subtree before cancelling any producer.
        # A leaf cancellation is delivered to its immediate parent first; if
        # only the public target were terminal, that delivery could resume an
        # intermediate suspended parent and create an untracked replacement
        # task while this finalizer awaited the original owners.
        for child_aid in descendants:
            child = self.table.get(child_aid)
            if child is not None and not child.state.phase.is_terminal():
                child.state.cancel("parent turn interrupted by user")
                if not child.result:
                    child.result = failure
        target.state.cancel(reason)
        target.result = failure

        owned_tasks = {
            task
            for child_aid in descendants
            for task in (
                self._tasks.get(child_aid),
                self._startup_tasks.get(child_aid),
            )
            if task is not None and not task.done()
        }
        for task in owned_tasks:
            task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)

        for child_aid in (*descendants, aid):
            scb = self.table.get(child_aid)
            if scb is None:
                continue
            table = scb.state.pending_events
            for tool_call_id, row in tuple(table.rows.items()):
                if row.status is RowStatus.PENDING:
                    table.fill(
                        tool_call_id,
                        result=failure,
                        status=RowStatus.FAILED,
                        error=failure,
                    )
            for message in table.ordered_results():
                scb.state.append_message(message)
            table.clear()
            scb.state.clear_active_turn()
            if not scb.state.phase.is_terminal():
                scb.state.cancel(
                    reason
                    if child_aid == aid
                    else "parent turn interrupted by user"
                )
            if child_aid == aid:
                scb.state.terminal_reason = reason
                scb.result = failure
            elif not scb.result:
                scb.result = failure
            self._spawn_origin.pop(child_aid, None)
            self._startup_origin.pop(child_aid, None)
            self._delivery_committed.discard(child_aid)
            self._release_leases(child_aid)
            self._turn_started_at.pop(child_aid, None)
            self._autosave_session(child_aid)

        await self._safe_emit_scheduler_event(
            self._events.agent_cancelled(aid, target.agent.name)
        )
        self._write_manifest()

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
