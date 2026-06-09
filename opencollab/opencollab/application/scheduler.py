"""Scheduler — passive process tracker for multi-agent execution.

Tracks every Session as a SessionControlBlock in a SessionTable:
- spawn() is non-blocking (returns aid immediately)
- Agents run in parallel via asyncio.create_task
- Results are delivered to parents via message injection into parent state

The class is assembled from three cohesive mixins, each in its own module:
- ``LifecycleMixin`` (``scheduler_lifecycle``) — register/spawn/drive/wake/deliver
- ``MessagingMixin`` (``scheduler_messaging``) — teammate-message inbox
- ``InflightDedupMixin`` (``scheduler_dedup``) — single-flight spawn dedup

This module keeps the run-loop orchestration, roster snapshots, and the shared
helpers (manifest/autosave, topology check, event emission) the mixins rely on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from opencollab.application.ports import (
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SessionFactoryPort,
    TracePort,
    WorktreePoolPort,
)
from opencollab.application.scheduler_dedup import InflightDedupMixin
from opencollab.application.scheduler_lifecycle import LifecycleMixin
from opencollab.application.scheduler_messaging import MessagingMixin
from opencollab.application.scheduler_types import LaunchSpec, QueuedTeammateMessage
from opencollab.application.self_collaboration import run_spawn_with_review
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.scheduler import SessionTable
from opencollab.domain.session import SessionPhase
from opencollab.domain.team import Topology

logger = logging.getLogger(__name__)

# Worktree diffs appended to a child's result are bounded so one huge diff
# can't blow up the parent's context; oversize diffs keep head + tail.
WORKTREE_DIFF_MAX_CHARS = 12_000
WORKTREE_DIFF_KEEP_CHARS = 6_000  # kept from each end when truncating


class Scheduler(LifecycleMixin, MessagingMixin, InflightDedupMixin):
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

    def _role_of(self, aid: int) -> str:
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else "?"

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
        if len(diff) > WORKTREE_DIFF_MAX_CHARS:
            diff = (
                diff[:WORKTREE_DIFF_KEEP_CHARS]
                + f"\n\n... [{len(diff) - WORKTREE_DIFF_MAX_CHARS} chars truncated] ...\n\n"
                + diff[-WORKTREE_DIFF_KEEP_CHARS:]
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
