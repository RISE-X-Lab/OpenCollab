"""Agent lifecycle for the Scheduler: register, spawn, drive, wake, deliver.

This is the heart of the passive scheduler — the non-blocking spawn, the
per-agent run/finalize loop, and the wake path that routes a finished child's
result back into the pending row that suspended its parent.

``LifecycleMixin`` is composed into ``Scheduler`` and relies on the maps and
helpers created in ``Scheduler.__init__`` (``table``, ``_sessions``,
``_tasks``, ``_locks``, ``_spawn_origin``, ``_session_factory``,
``_worktree_pool``, ``_tracer``, and the dedup / messaging / event / topology
helpers).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from opencollab.application.scheduler_types import LaunchSpec
from opencollab.domain.pending import PendingRowError, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)


class LifecycleMixin:
    """Spawn, drive, finalize, and wake agents in the delegation tree."""

    def register_lead(self, session: Any) -> int:
        """Register an already-built session as agent 0 (aid=0).

        Low-level primitive: assigns aid, marks SCHEDULED, adds the SCB, and
        stores the lead handle. ``create_init_process`` builds the session and
        delegates here; tests can register a pre-built (or fake) lead directly.
        """
        aid = self.table.allocate_aid()  # = 0
        session.state.aid = aid
        session.state.set_phase(SessionPhase.SCHEDULED)
        scb = SessionControlBlock(
            aid=aid,
            parent_aid=None,
            agent=session.agent,
            state=session.state,
        )
        self.table.add(scb)
        self._sessions[aid] = session
        self._lead_session = session
        # Seed the running allocation with the Lead's reserve so the first child
        # is granted from the pool minus the Lead's headroom.
        self._seed_lead_reservation()
        self._write_manifest()
        return aid

    def create_init_process(self, launch: LaunchSpec) -> int:
        """Create and register agent 0 — the init process (aid=0).

        The factory owns construction (env, tools, prompt, store); the
        scheduler owns the launch lifecycle: build via the factory, apply the
        launch spec (resume or seed), then register. The root-process mirror of
        ``spawn``.
        """
        session = self._session_factory.create_lead_session(
            scheduler=self,
            launch=launch,
            budget=self._max_budget_tokens,
        )
        session.apply_launch(launch)
        return self.register_lead(session)

    async def spawn(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str = "",
        tool_call_id: str | None = None,
    ) -> int:
        """Non-blocking spawn. Creates SCB, builds session, starts task. Returns aid.

        When ``tool_call_id`` is given (a deferred ``spawn_agent`` tool call),
        the (parent, tool_call_id) origin is recorded so the child's completion
        fills the parent's pending row and re-activates it. Without it the spawn
        is fire-and-forget (the result lives only in the child's SCB).

        Raises ``PermissionError`` if the team topology forbids ``parent_aid``'s
        role from spawning ``role``; the tool executor turns that into a tool
        result so the parent's run loop continues uninterrupted.
        """
        self._check_topology(parent_aid, role, verb="spawn")
        aid = self.table.allocate_aid()
        # Reserve this (role, task) AND its budget synchronously — before the
        # first await — so a duplicate / batched spawn later in the same
        # tool-call batch already sees the updated allocation and cannot
        # oversubscribe the global pool.
        self._reserve_inflight(aid, role, task)
        budget = self._reserve_child_budget(aid)

        # Build environment
        env = await self._worktree_pool.acquire(role)

        # Build session via factory. The task is seeded as the agent's first
        # user-context message (the TASK-layer ContextSource) inside the factory,
        # so the whole startup context is assembled in one place.
        session = self._session_factory.build_spawn_session(
            role=role,
            env=env,
            budget=budget,
            aid=aid,
            scheduler=self,
            task=task,
            context=context,
        )
        session.state.set_phase(SessionPhase.SCHEDULED)

        # Create SCB
        scb = SessionControlBlock(
            aid=aid,
            parent_aid=parent_aid,
            agent=session.agent,
            state=session.state,
        )
        self.table.add(scb)
        self._sessions[aid] = session
        if tool_call_id is not None:
            self._spawn_origin[aid] = (parent_aid, tool_call_id)

        # Emit spawn event
        await self.emit_scheduler_event(
            self._events.agent_spawned(aid, parent_aid, role, task)
        )

        # Start async task
        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))

        self._write_manifest()
        self._autosave_session(parent_aid)
        return aid

    def _release_reservations(self, aid: int) -> None:
        """Release a terminal child's single-flight and budget reservations.

        Both are held from spawn until the child reaches a terminal phase; this
        frees them together so a later spawn can reuse the (role, task) key and
        the unspent budget headroom. Idempotent at each site.
        """
        self._clear_inflight(aid)
        self._release_child_budget(aid)

    async def _drive_agent(self, aid: int, session: Any) -> None:
        """Run a session's loop once and finalize.

        The loop returns either suspended on ``AWAITING_EVENTS`` (the session
        spawned its own children — leave it; a child wake re-enters here later)
        or terminal. On terminal completion it emits ``agent_completed`` and, if
        the session was itself a deferred child, fills its parent's pending row
        and re-activates the parent. Used for both the initial run and every
        event-driven resume, at any depth of the delegation tree.
        """
        scb = self.table.get(aid)
        if scb is None:
            return

        start = time.monotonic()

        try:
            result = await session.run_loop()
        except asyncio.CancelledError:
            self._release_reservations(aid)
            scb.state.cancel()
            await self.emit_scheduler_event(
                self._events.agent_cancelled(aid, scb.agent.name)
            )
            raise
        except Exception as exc:
            self._release_reservations(aid)
            scb.state.fail()
            scb.result = f"Error: {exc}"
            await self.emit_scheduler_event(
                self._events.agent_failed(aid, scb.agent.name, str(exc))
            )
            await self._deliver_to_parent(aid, f"Error: {exc}", RowStatus.FAILED)
            await self._drain_message_inbox(aid, allow_current_task=True)
            return

        scb.result = result

        # Suspended on its own deferred work — not finished. A child's wake will
        # re-enter _drive_agent and finalize once it reaches a terminal phase.
        # The reservation stays held: the task is still genuinely in flight.
        if scb.state.phase is SessionPhase.AWAITING_EVENTS:
            return

        # Terminal — release the single-flight + budget reservations before
        # delivering, so a later spawn can reuse this child's unspent headroom.
        self._release_reservations(aid)

        # Append worktree diff if available (delivered to the parent / read by
        # spawn_with_review; scb.result keeps the pre-diff run-loop result).
        env = getattr(session, "env", None)
        if env is not None:
            result = await self._append_worktree_diff(env, result)

        latency = time.monotonic() - start

        if self._tracer:
            self._tracer.log_step(
                step_type="agent_completed",
                payload={"aid": aid, "role": scb.agent.name, "result_len": len(result)},
                tokens=session.used_tokens,
                latency=latency,
            )

        await self.emit_scheduler_event(
            self._events.agent_completed(
                aid, scb.parent_aid, scb.agent.name, latency, len(result)
            )
        )

        status = RowStatus.FAILED if scb.state.phase is SessionPhase.ERROR else RowStatus.DONE
        await self._deliver_to_parent(aid, result, status)
        await self._drain_message_inbox(aid, allow_current_task=True)

    async def _deliver_to_parent(self, child_aid: int, result: str, status: RowStatus) -> None:
        """Route a finished child's result to the pending row that suspended its
        parent, then re-activate the parent. No-op for fire-and-forget spawns.
        """
        origin = self._spawn_origin.pop(child_aid, None)
        if origin is None:
            return
        parent_aid, tool_call_id = origin
        try:
            await self._wake(parent_aid, tool_call_id, result, status)
        except PendingRowError as exc:
            # A misrouted completion must surface loudly, never silently succeed.
            logger.error("misrouted completion from child %s: %s", child_aid, exc)
            await self.emit_scheduler_event(
                self._events.agent_failed(parent_aid, self._role_of(parent_aid), str(exc))
            )

    async def _wake(self, parent_aid: int, tool_call_id: str, result: str, status: RowStatus) -> None:
        """Fill the parent's pending row and, if that completes the batch while
        the parent is suspended, create a resume task. Fill + completeness check
        + task creation run under one per-parent lock so concurrent child
        completions can never double-wake the parent. Raises ``PendingRowError``
        on an unknown/already-filled tool_call_id.
        """
        parent_scb = self.table.get(parent_aid)
        parent_session = self._sessions.get(parent_aid)
        if parent_scb is None or parent_session is None:
            return

        lock = self._locks.setdefault(parent_aid, asyncio.Lock())
        async with lock:
            table = parent_scb.state.pending_events
            table.fill(tool_call_id, result=result, status=status)
            in_flight = self._tasks.get(parent_aid)
            should_resume = (
                parent_scb.state.phase is SessionPhase.AWAITING_EVENTS
                and table.is_complete()
                and (in_flight is None or in_flight.done())
            )
            if should_resume:
                self._tasks[parent_aid] = asyncio.create_task(
                    self._drive_agent(parent_aid, parent_session)
                )

        if should_resume:
            await self.emit_scheduler_event(
                self._events.agent_resumed(parent_aid, self._role_of(parent_aid))
            )
