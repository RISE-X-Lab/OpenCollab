"""Scheduler — passive process tracker for multi-agent execution.

Tracks every Session as a SessionControlBlock in a SessionTable:
- spawn() is non-blocking (returns aid immediately)
- Agents run in parallel via asyncio.create_task
- Results are delivered to parents via message injection into parent state
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable
from xml.sax.saxutils import escape, quoteattr

from opencollab.application.ports import (
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SessionFactoryPort,
    TracePort,
    WorktreePoolPort,
)
from opencollab.application.scheduler_types import LaunchSpec, QueuedTeammateMessage
from opencollab.application.self_collaboration import run_spawn_with_review
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.pending import PendingRowError, RowStatus
from opencollab.domain.scheduler import SessionControlBlock, SessionTable, split_budget
from opencollab.domain.session import SessionPhase
from opencollab.domain.team import Topology

logger = logging.getLogger(__name__)


class Scheduler:
    """Passive scheduler that tracks SCBs and runs agents in parallel.

    Design:
    - SessionTable holds all SCBs (pure data, no I/O)
    - spawn() creates a new SCB and schedules it for execution
    - run() drives the main loop, waiting for all agents to complete
    - Results are injected into parent sessions as system messages
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactoryPort,
        worktree_pool: WorktreePoolPort,
        event_sink: EventPublisherPort | None = None,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 500_000,
        permission_policy: PermissionPort | None = None,
        topology: Topology | None = None,
        roles: tuple[str, ...] = (),
    ):
        self._session_factory = session_factory
        self._worktree_pool = worktree_pool
        self._event_sink = event_sink
        self._tracer = tracer
        self._max_budget_tokens = max_budget_tokens
        self._permission_policy = permission_policy
        self._topology = topology
        # Configured role names (from the team config), in declaration order.
        # Used by ``team_roster`` to surface the team before anything spawns.
        self._roles = roles

        self.table = SessionTable()
        self._tasks: dict[int, asyncio.Task] = {}
        self._sessions: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._lead_session: Any | None = None
        # child aid -> (parent aid, tool_call_id) for deferred spawns: lets a
        # child's completion fill the exact pending row that suspended its
        # parent. Absent for legacy fire-and-forget spawns (tool_call_id=None).
        self._spawn_origin: dict[int, tuple[int, str]] = {}
        # Single-flight spawn dedup: task key -> the aid currently handling it,
        # plus the reverse map for release. A (role, task) is reserved at spawn
        # and freed when the child reaches a terminal phase, so a model that
        # re-issues an identical spawn is refused (see ``inflight_spawn``) rather
        # than spinning up a duplicate — tool-level enforcement of "don't spawn
        # the same task twice", which prompt guidance alone cannot guarantee.
        self._inflight: dict[str, int] = {}
        self._inflight_key_of: dict[int, str] = {}
        # aid -> queued teammate messages waiting to be appended as user
        # messages once that session is not running or suspended on pending work.
        self._message_inbox: dict[int, list[QueuedTeammateMessage]] = {}
        # Optional bootstrap-injected callback that persists a team.json manifest
        # from the current roster. Kept as a callback so the application layer
        # stays free of filesystem I/O.
        self._manifest_writer: Callable[[], None] | None = None

    def set_manifest_writer(self, fn: Callable[[], None]) -> None:
        """Inject the team-manifest persister (called on every roster change)."""
        self._manifest_writer = fn

    def _write_manifest(self) -> None:
        if self._manifest_writer is None:
            return
        try:
            self._manifest_writer()
        except Exception as exc:  # best-effort, mirrors AutoSaveSubscriber
            logger.debug("manifest write failed: %s", exc)

    def _autosave_session(self, aid: int) -> None:
        session = self._sessions.get(aid)
        if session is None:
            return
        save_path = getattr(session, "auto_save_path", None)
        save = getattr(session, "save", None)
        if not save_path or not callable(save):
            return
        try:
            save(save_path)
        except Exception as exc:  # best-effort, mirrors AutoSaveSubscriber
            logger.debug("session auto-save failed for aid %s: %s", aid, exc)

    def _autosave_all_sessions(self) -> None:
        for aid in list(self._sessions):
            self._autosave_session(aid)

    @property
    def used_tokens(self) -> int:
        """Total tokens across all agents."""
        return self.table.total_used_tokens

    @property
    def lead_session(self) -> Any:
        """Agent 0's session (the interactive entry)."""
        return self._lead_session

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
        # Reserve this (role, task) synchronously — before the first await — so a
        # duplicate spawn later in the same tool-call batch already sees it.
        self._reserve_inflight(aid, role, task)

        # Build environment
        env = await self._worktree_pool.acquire(role)
        budget = split_budget(self._max_budget_tokens, self.used_tokens)

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
        await self._emit_scheduler_event(
            "agent_spawned",
            {"aid": aid, "parent_aid": parent_aid, "role": role, "task": task[:100]},
        )

        # Start async task
        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))

        self._write_manifest()
        self._autosave_session(parent_aid)
        return aid

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
            self._clear_inflight(aid)
            scb.state.cancel()
            await self._emit_scheduler_event(
                "agent_cancelled",
                {"aid": aid, "role": scb.agent.name},
            )
            raise
        except Exception as exc:
            self._clear_inflight(aid)
            scb.state.fail()
            scb.result = f"Error: {exc}"
            await self._emit_scheduler_event(
                "agent_failed",
                {"aid": aid, "role": scb.agent.name, "error": str(exc)},
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

        # Terminal — release the single-flight reservation before delivering.
        self._clear_inflight(aid)

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

        await self._emit_scheduler_event(
            "agent_completed",
            {
                "aid": aid,
                "parent_aid": scb.parent_aid,
                "role": scb.agent.name,
                "latency": latency,
                "result_len": len(result),
            },
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
            await self._emit_scheduler_event(
                "agent_failed",
                {"aid": parent_aid, "role": self._role_of(parent_aid), "error": str(exc)},
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
            await self._emit_scheduler_event(
                "agent_resumed",
                {"aid": parent_aid, "role": self._role_of(parent_aid)},
            )

    def _role_of(self, aid: int) -> str:
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else "?"

    @staticmethod
    def _task_key(role: str, task: str) -> str:
        """Stable dedup key for a (role, task). Whitespace is collapsed so a
        reflowed re-prompt of the same instruction maps to the same key.
        """
        normalized = " ".join(task.split())
        return hashlib.md5(f"{role}\x00{normalized}".encode()).hexdigest()

    def inflight_spawn(self, role: str, task: str) -> int | None:
        """The aid already handling this (role, task) if a spawn is in flight,
        else ``None``. The spawn tool consults this to refuse duplicates.
        """
        return self._inflight.get(self._task_key(role, task))

    def _reserve_inflight(self, aid: int, role: str, task: str) -> None:
        key = self._task_key(role, task)
        self._inflight[key] = aid
        self._inflight_key_of[aid] = key

    def _clear_inflight(self, aid: int) -> None:
        """Release a child's reservation once it is terminal (idempotent)."""
        key = self._inflight_key_of.pop(aid, None)
        if key is not None and self._inflight.get(key) == aid:
            del self._inflight[key]

    def _check_topology(self, src_aid: int, dst_role: str, *, verb: str) -> None:
        """Raise ``PermissionError`` if the topology forbids src → dst_role."""
        if self._topology is None:
            return
        src_role = self._role_of(src_aid)
        if not self._topology.allows(src_role, dst_role):
            raise PermissionError(
                f"Role '{src_role}' is not permitted to {verb} '{dst_role}' "
                f"under the team topology."
            )

    async def send_message(self, from_aid: int, to_aid: int, summary: str, content: str) -> str:
        """Queue a teammate message for async delivery and return immediately.

        The recipient sees the message as a normal user turn with an XML
        envelope. If the recipient is idle, it is scheduled in the background;
        if it is running or awaiting delegated work, the message stays in an
        out-of-history inbox until the session can safely accept another user
        turn.
        """
        if to_aid == from_aid:
            return "Error: an agent cannot message itself."
        target = self._sessions.get(to_aid)
        if target is None:
            return f"Error: no agent with aid {to_aid}."
        if self._topology is not None and not self._topology.allows(
            self._role_of(from_aid), self._role_of(to_aid)
        ):
            return (
                f"Error: role '{self._role_of(from_aid)}' is not permitted to "
                f"message '{self._role_of(to_aid)}' under the team topology."
            )

        lock = self._locks.setdefault(to_aid, asyncio.Lock())
        async with lock:
            message = QueuedTeammateMessage(
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                xml=self._format_teammate_message(from_aid, summary, content),
            )
            self._message_inbox.setdefault(to_aid, []).append(message)
            target.state.queue_pending_user_message(
                {
                    "role": "user",
                    "content": message.xml,
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "summary": summary,
                }
            )
            self._autosave_session(to_aid)
            await self._emit_scheduler_event(
                "agent_message_sent",
                {
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "role": self._role_of(to_aid),
                    "summary": summary,
                },
            )
            await self._drain_message_inbox_locked(to_aid)
        return f"Message queued to aid {to_aid}."

    @staticmethod
    def _format_teammate_message(from_aid: int, summary: str, content: str) -> str:
        sender = f"A{from_aid}"
        return (
            f"<teammate-message teammate_id={quoteattr(sender)} "
            f"summary={quoteattr(summary)}>\n"
            f"{escape(content)}\n"
            "</teammate-message>"
        )

    async def _drain_message_inbox(self, aid: int, *, allow_current_task: bool = False) -> None:
        lock = self._locks.setdefault(aid, asyncio.Lock())
        async with lock:
            await self._drain_message_inbox_locked(aid, allow_current_task=allow_current_task)

    async def _drain_message_inbox_locked(
        self,
        aid: int,
        *,
        allow_current_task: bool = False,
    ) -> None:
        inbox = self._message_inbox.get(aid)
        if not inbox:
            return
        session = self._sessions.get(aid)
        scb = self.table.get(aid)
        if session is None or scb is None:
            return
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return
        if scb.state.phase is SessionPhase.AWAITING_EVENTS or not scb.state.pending_events.is_empty():
            return

        messages = list(inbox)
        inbox.clear()
        for message in messages:
            session.state.discard_pending_user_message(message.xml)
            await session.add_user_message(message.xml)
            await self._emit_scheduler_event(
                "agent_message_delivered",
                {
                    "from_aid": message.from_aid,
                    "to_aid": message.to_aid,
                    "summary": message.summary,
                    "content_len": len(message.content),
                },
            )
        self._autosave_session(aid)

        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))

    def team_snapshot(self) -> list[dict[str, Any]]:
        """Read-only roster of every tracked (live) agent, ordered by aid."""
        snapshot: list[dict[str, Any]] = []
        for aid in sorted(self.table.entries):
            scb = self.table.entries[aid]
            task = self._tasks.get(aid)
            snapshot.append(
                {
                    "aid": aid,
                    "role": scb.agent.name,
                    "parent_aid": scb.parent_aid,
                    "phase": scb.state.phase.value,
                    "busy": task is not None and not task.done(),
                }
            )
        return snapshot

    def team_roster(self) -> list[dict[str, Any]]:
        """Full configured team for the prompt toolbar: every live agent plus
        each configured role that has no live agent yet (``aid=None``, phase
        ``"available"``). Unlike ``team_snapshot`` (live agents only, used to
        message teammates by aid), this surfaces the team the user defined in
        the team config before anything has spawned.
        """
        live = self.team_snapshot()
        live_roles = {entry["role"] for entry in live}
        available = [
            {
                "aid": None,
                "role": role,
                "parent_aid": None,
                "phase": "available",
                "busy": False,
            }
            for role in self._roles
            if role not in live_roles
        ]
        return live + available

    async def _append_worktree_diff(self, env: EnvironmentPort, result: str) -> str:
        """If env is a worktree, append its diff to the result."""
        get_diff = getattr(env, "get_diff", None)
        if not callable(get_diff):
            return result
        diff = await get_diff()
        if not diff:
            return result
        if len(diff) > 12_000:
            diff = (
                diff[:6_000]
                + f"\n\n... [{len(diff) - 12_000} chars truncated] ...\n\n"
                + diff[-6_000:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def _emit_scheduler_event(self, event_type: str, data: dict) -> None:
        """Emit a scheduler event via the event sink."""
        if self._event_sink is None:
            return
        event = SchedulerEvent(type=event_type, data=data)
        if hasattr(self._event_sink, "emit"):
            await self._event_sink.emit(event)
        else:
            result = self._event_sink(event)
            if asyncio.iscoroutine(result):
                await result

    async def run(self, user_message: str) -> str:
        """Send message to lead and run until the whole team is quiescent.

        A session that suspends on deferred work (``AWAITING_EVENTS``) returns
        its task while its children run; a child's completion re-activates it
        with a fresh task. So "all tasks done" is no longer the end — the team
        is finished only when no task is running, no pending table is
        outstanding, and every session is terminal or idle (``_quiescent``).
        """
        if self._lead_session is None:
            raise RuntimeError("Scheduler has no lead session. Call create_init_process() first.")

        await self._lead_session.add_user_message(user_message)
        self._tasks[0] = asyncio.create_task(self._drive_agent(0, self._lead_session))

        while True:
            for done_aid in [a for a, t in self._tasks.items() if t.done()]:
                del self._tasks[done_aid]
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

        # Return lead's last assistant message content
        for msg in reversed(self._lead_session.state.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"]
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

    async def cleanup(self) -> None:
        """Cancel pending tasks and clean up worktree environments."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._autosave_all_sessions()
        self._write_manifest()
        await self._worktree_pool.release()

    async def spawn_with_review(
        self,
        parent_aid: int,
        task: str,
        context: str = "",
        max_iterations: int = 3,
    ) -> str:
        """Self-Collaboration: Coder -> Reviewer loop (see ``self_collaboration``)."""
        return await run_spawn_with_review(
            self, parent_aid, task, context, max_iterations
        )


__all__ = ["LaunchSpec", "QueuedTeammateMessage", "Scheduler"]
