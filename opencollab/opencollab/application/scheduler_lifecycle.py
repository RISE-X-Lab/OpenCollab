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
from opencollab.domain.identity import validate_role_identity
from opencollab.domain.pending import PendingRowError, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)


class LifecycleMixin:
    """Spawn, drive, finalize, and wake agents in the delegation tree."""

    def register_lead(self, session: Any) -> int:
        """Register an already-built session as agent 0 (aid=0).

        Low-level primitive: assigns aid, schedules a fresh IDLE session, adds
        the SCB, and stores the lead handle. A restored durable phase is kept so
        AWAITING_EVENTS rows can drain before the next user turn.
        """
        session.agent.name = validate_role_identity(session.agent.name)
        aid = self.table.allocate_aid()  # = 0
        session.state.aid = aid
        scb = SessionControlBlock(
            aid=aid,
            parent_aid=None,
            agent=session.agent,
            state=session.state,
        )
        self.table.add(scb)
        self._sessions[aid] = session
        self._lead_session = session
        self._restore_message_inbox(aid, session.state)
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
        if self._shutting_down:
            raise RuntimeError("Cannot spawn agent: scheduler is shutting down.")
        if self.table.get(parent_aid) is None or parent_aid not in self._sessions:
            raise ValueError(f"Cannot spawn agent: no parent with aid {parent_aid}.")
        role = validate_role_identity(role)
        self._check_topology(parent_aid, role, verb="spawn")
        aid = self.table.allocate_aid()
        startup_task = asyncio.current_task()
        if startup_task is not None:
            self._startup_tasks[aid] = startup_task
        if tool_call_id is not None:
            self._startup_origin[aid] = (parent_aid, tool_call_id)
        env: Any | None = None
        parent_lease: tuple[int, int] | None = None
        # Reserve this (role, task) AND its budget synchronously — before the
        # first await — so a duplicate / batched spawn later in the same
        # tool-call batch already sees the updated allocation and cannot
        # oversubscribe the global pool.
        # Everything from reservation until the driver task is scheduled may raise
        # (worktree acquire, session build, event emission). The driver task owns
        # releasing the two reservations on termination — but it only exists once
        # ``create_task`` below succeeds. So if anything raises before that, the
        # reservations would leak permanently (the budget grant inflates the pool
        # for every future spawn, and the inflight key permanently refuses any
        # re-spawn of this (role, task)). Release both and re-raise so the caller
        # (execute_deferred) still surfaces the failure into the parent's row.
        try:
            # ``spawn`` runs after the parent's current model generation has
            # completed. Return the parent's unused turn lease before granting
            # children; the parent will acquire a fresh lease when the complete
            # pending batch wakes it for its next generation.
            parent_driver = self._tasks.get(parent_aid)
            parent_scb = self.table.get(parent_aid)
            if (
                parent_driver is not None
                and not parent_driver.done()
                and parent_scb is not None
                and (
                    parent_driver is asyncio.current_task()
                    or parent_scb.state.phase is SessionPhase.EXECUTING_TOOLS
                )
            ):
                parent_lease = self._release_turn_budget(parent_aid)
                if parent_lease is not None:
                    self._track_review_parent_lease_release(parent_aid, 1)
            self._reserve_inflight(aid, role, task)
            budget = self._reserve_child_budget(aid)
            if budget <= 0:
                raise RuntimeError(
                    "Cannot spawn agent: team token budget is fully allocated."
                )

            # Build environment
            env = await self._worktree_pool.acquire(role)
            self._startup_envs[aid] = env
            if self._shutting_down:
                raise RuntimeError("Cannot spawn agent: scheduler is shutting down.")

            # Build session via factory. The task is seeded as the agent's first
            # user-context message (the TASK-layer ContextSource) inside the
            # factory, so the whole startup context is assembled in one place.
            session = self._session_factory.build_spawn_session(
                role=role,
                env=env,
                budget=budget,
                aid=aid,
                scheduler=self,
                task=task,
                context=context,
            )
            session.agent.name = role

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
                self._startup_origin.pop(aid, None)

            # Emit spawn event
            await self.emit_scheduler_event(
                self._events.agent_spawned(aid, parent_aid, role, task)
            )
            if self._shutting_down:
                raise RuntimeError("Cannot spawn agent: scheduler is shutting down.")

            # Start async task. Once this succeeds, _drive_agent owns the
            # reservation release — must be the last statement that can hand off
            # ownership, so the except below never double-releases on success.
            self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))
            self._startup_tasks.pop(aid, None)
            self._startup_envs.pop(aid, None)
            self._startup_origin.pop(aid, None)
        except asyncio.CancelledError:
            # Driver task was never scheduled, so nothing will release these.
            await self._rollback_failed_spawn(aid, env)
            if not self._shutting_down:
                self._restore_turn_budget(parent_aid, parent_lease)
                if parent_lease is not None:
                    self._track_review_parent_lease_release(parent_aid, -1)
            if tool_call_id is not None:
                await self._fail_cancelled_origin(
                    parent_aid,
                    tool_call_id,
                    "agent spawn cancelled before startup completed",
                )
            raise
        except BaseException:
            await self._rollback_failed_spawn(aid, env)
            if not self._shutting_down:
                self._restore_turn_budget(parent_aid, parent_lease)
                if parent_lease is not None:
                    self._track_review_parent_lease_release(parent_aid, -1)
            raise

        self._write_manifest()
        self._autosave_session(parent_aid)
        return aid

    async def _rollback_failed_spawn(self, aid: int, env: Any | None) -> None:
        """Undo every side effect created before a child driver task exists."""
        self._release_reservations(aid)
        self.table.entries.pop(aid, None)
        self._sessions.pop(aid, None)
        self._spawn_origin.pop(aid, None)
        self._startup_tasks.pop(aid, None)
        self._startup_envs.pop(aid, None)
        self._startup_origin.pop(aid, None)
        self._tasks.pop(aid, None)
        self._locks.pop(aid, None)
        self._message_inbox.pop(aid, None)

        if env is None:
            return
        try:
            await self._worktree_pool.release_env(env)
        except Exception as exc:
            logger.error("failed-spawn environment cleanup failed for aid %s: %s", aid, exc)

    async def _fail_cancelled_origin(
        self, parent_aid: int, tool_call_id: str, reason: str
    ) -> None:
        """Resolve a pre-driver cancellation when its pending row already exists."""
        parent = self.table.get(parent_aid)
        if parent is None or tool_call_id not in parent.state.pending_events.rows:
            return
        try:
            await self._wake(
                parent_aid,
                tool_call_id,
                f"Error: {reason}",
                RowStatus.FAILED,
            )
        except PendingRowError:
            logger.error(
                "cancelled spawn could not fail pending row %s on parent %s",
                tool_call_id,
                parent_aid,
            )

    def _release_reservations(self, aid: int) -> None:
        """Release a terminal child's single-flight and budget reservations.

        Both are held from spawn until the child reaches a terminal phase; this
        frees them together so a later spawn can reuse the (role, task) key and
        the unspent budget headroom. Idempotent at each site.
        """
        self._clear_inflight(aid)
        self._release_turn_budget(aid)

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
            reason = "Error: agent cancelled before completing delegated work"
            scb.result = reason
            try:
                await self.emit_scheduler_event(
                    self._events.agent_cancelled(aid, scb.agent.name)
                )
            except Exception as event_exc:
                logger.error(
                    "agent_cancelled event failed for aid %s: %s", aid, event_exc
                )
            await self._deliver_to_parent(aid, reason, RowStatus.FAILED)
            if not self._shutting_down:
                await self._drain_message_inbox(aid, allow_current_task=True)
                await self._drain_ready_message_inboxes()
            raise
        except Exception as exc:
            self._release_reservations(aid)
            scb.state.fail()
            scb.result = f"Error: {exc}"
            try:
                await self.emit_scheduler_event(
                    self._events.agent_failed(aid, scb.agent.name, str(exc))
                )
            except Exception as event_exc:
                logger.error(
                    "agent_failed event failed for aid %s: %s", aid, event_exc
                )
            await self._deliver_to_parent(aid, f"Error: {exc}", RowStatus.FAILED)
            await self._drain_message_inbox(aid, allow_current_task=True)
            await self._drain_ready_message_inboxes()
            return

        # A cancellation-resistant provider/session can outlive the scheduler's
        # bounded teardown. Cleanup has already revoked its environment and
        # assigned a failure terminal; discard any late return before it can
        # overwrite that state or publish a successful completion.
        if self._shutting_down:
            self._finalize_cleanup_failure(aid)
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

        # A completed coding task still needs its patch evidence. Tracing and
        # event delivery are observational, but a missing diff is a technical
        # failure because the parent cannot verify what changed.
        env = getattr(session, "env", None)
        if env is not None:
            try:
                result = await self._append_worktree_diff(env, result)
            except Exception as exc:
                logger.error("worktree diff failed for aid %s: %s", aid, exc)
                scb.state.fail()
                result = f"Error: worktree diff extraction failed: {exc}"
                scb.result = result
                await self._safe_emit_scheduler_event(
                    self._events.agent_failed(aid, scb.agent.name, str(exc))
                )
                await self._deliver_to_parent(aid, result, RowStatus.FAILED)
                await self._drain_message_inbox(aid, allow_current_task=True)
                if not self._shutting_down:
                    await self._drain_ready_message_inboxes()
                return

        if self._shutting_down:
            self._finalize_cleanup_failure(aid)
            return

        # Store the same post-diff artifact that is delivered to the parent so
        # spawn_with_review gives the reviewer the actual implementation diff.
        scb.result = result

        latency = time.monotonic() - start

        if self._tracer:
            try:
                self._tracer.log_step(
                    step_type="agent_completed",
                    payload={"aid": aid, "role": scb.agent.name, "result_len": len(result)},
                    tokens=session.used_tokens,
                    latency=latency,
                )
            except Exception as exc:
                logger.error("agent_completed trace failed for aid %s: %s", aid, exc)

        try:
            await self.emit_scheduler_event(
                self._events.agent_completed(
                    aid, scb.parent_aid, scb.agent.name, latency, len(result)
                )
            )
        except Exception as exc:
            logger.error("agent_completed event failed for aid %s: %s", aid, exc)

        if self._shutting_down:
            self._finalize_cleanup_failure(aid)
            return

        status = RowStatus.FAILED if scb.state.phase is SessionPhase.ERROR else RowStatus.DONE
        await self._deliver_to_parent(aid, result, status)
        await self._drain_message_inbox(aid, allow_current_task=True)
        if not self._shutting_down:
            await self._drain_ready_message_inboxes()

    async def _deliver_to_parent(self, child_aid: int, result: str, status: RowStatus) -> None:
        """Route a finished child's result to the pending row that suspended its
        parent, then re-activate the parent. No-op for fire-and-forget spawns.
        """
        origin = self._spawn_origin.get(child_aid)
        if origin is None:
            return
        parent_aid, tool_call_id = origin
        try:
            await self._wake(
                parent_aid,
                tool_call_id,
                result,
                status,
                child_aid=child_aid,
            )
        except PendingRowError as exc:
            self._spawn_origin.pop(child_aid, None)
            # A misrouted completion must surface loudly, never silently succeed.
            logger.error("misrouted completion from child %s: %s", child_aid, exc)
            await self._safe_emit_scheduler_event(
                self._events.agent_failed(parent_aid, self._role_of(parent_aid), str(exc))
            )

    async def _wake(
        self,
        parent_aid: int,
        tool_call_id: str,
        result: str,
        status: RowStatus,
        *,
        child_aid: int | None = None,
    ) -> None:
        """Fill the parent's pending row and, if that completes the batch while
        the parent is suspended, create a resume task. Fill + completeness check
        + task creation run under one per-parent lock so concurrent child
        completions can never double-wake the parent. Raises ``PendingRowError``
        on an unknown/already-filled tool_call_id.
        """
        parent_scb = self.table.get(parent_aid)
        parent_session = self._sessions.get(parent_aid)
        if parent_scb is None or parent_session is None:
            if child_aid is not None:
                self._spawn_origin.pop(child_aid, None)
            return

        lock = self._locks.setdefault(parent_aid, asyncio.Lock())
        async with lock:
            table = parent_scb.state.pending_events
            cleanup_forced = False
            fill_error: str | None = None
            if child_aid is not None:
                child_scb = self.table.get(child_aid)
                cleanup_forced = self._shutting_down
                if cleanup_forced:
                    result = "Error: scheduler cleanup cancelled delegated work"
                    status = RowStatus.FAILED
                    fill_error = result
                    if child_scb is not None:
                        child_scb.state.cancel(result)
                        child_scb.result = result
            table.fill(
                tool_call_id,
                result=result,
                status=status,
                error=fill_error,
            )
            if child_aid is not None:
                self._spawn_origin.pop(child_aid, None)
                if not cleanup_forced:
                    self._delivery_committed.add(child_aid)
            in_flight = self._tasks.get(parent_aid)
            should_resume = (
                not self._shutting_down
                and
                parent_scb.state.phase is SessionPhase.AWAITING_EVENTS
                and table.is_complete()
                and (in_flight is None or in_flight.done())
            )
            if should_resume:
                self._reserve_turn_budget(parent_aid)
                self._tasks[parent_aid] = asyncio.create_task(
                    self._drive_agent(parent_aid, parent_session)
                )
            elif (
                not self._shutting_down
                and
                parent_scb.state.phase is SessionPhase.AWAITING_EVENTS
                and table.is_complete()
                and in_flight is not None
                and not in_flight.done()
            ):
                in_flight.add_done_callback(
                    lambda finished: asyncio.create_task(
                        self._resume_after_parent_task(
                            parent_aid,
                            parent_session,
                            finished,
                        )
                    )
                )

        if should_resume:
            await self._safe_emit_scheduler_event(
                self._events.agent_resumed(parent_aid, self._role_of(parent_aid))
            )

    async def _resume_after_parent_task(
        self,
        parent_aid: int,
        parent_session: Any,
        finished_task: asyncio.Task,
    ) -> None:
        """Close the tail race between AWAITING_EVENTS and driver completion."""
        parent_scb = self.table.get(parent_aid)
        if parent_scb is None:
            return
        lock = self._locks.setdefault(parent_aid, asyncio.Lock())
        should_resume = False
        async with lock:
            current = self._tasks.get(parent_aid)
            no_active_replacement = (
                current is None or current is finished_task or current.done()
            )
            if (
                not self._shutting_down
                and
                no_active_replacement
                and parent_scb.state.phase is SessionPhase.AWAITING_EVENTS
                and parent_scb.state.pending_events.is_complete()
            ):
                self._reserve_turn_budget(parent_aid)
                self._tasks[parent_aid] = asyncio.create_task(
                    self._drive_agent(parent_aid, parent_session)
                )
                should_resume = True
        if should_resume:
            try:
                await self.emit_scheduler_event(
                    self._events.agent_resumed(parent_aid, self._role_of(parent_aid))
                )
            except Exception as exc:
                logger.error("agent_resumed event failed for aid %s: %s", parent_aid, exc)
