"""Scheduler team views, topology checks, diffs, and event emission."""

from __future__ import annotations

import logging
from typing import Any

from opencollab.application._scheduler_constants import (
    WORKTREE_DIFF_KEEP_CHARS,
    WORKTREE_DIFF_MAX_CHARS,
)
from opencollab.application.ports import DiffCapablePort, EnvironmentPort
from opencollab.domain.events import SchedulerEvent

logger = logging.getLogger(__name__)


class SchedulerTeamMixin:
    @property
    def lead_session(self) -> Any:
        """Agent 0's session (the interactive entry)."""
        return self._lead_session

    def _role_of(self, aid: int) -> str:
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else "?"

    def _topology_forbids(self, src_role: str, dst_role: str) -> bool:
        """True when a team topology is configured and forbids ``src_role`` → ``dst_role``.

        The single deny decision shared by the two verbs — spawn
        (``_check_topology`` raises ``PermissionError``) and ``send_message``
        (returns an error-string tool result). Each caller keeps its own role
        resolution and its own response shape; only the boolean is shared, so
        ``allows`` is consulted exactly one way for both. Takes role *strings*,
        not aids: a spawn target has no aid yet.
        """
        return self._topology is not None and not self._topology.allows(src_role, dst_role)

    def _check_topology(self, src_aid: int, dst_role: str, *, verb: str) -> None:
        """Raise ``PermissionError`` if the topology forbids src → dst_role."""
        src_role = self._role_of(src_aid)
        if self._topology_forbids(src_role, dst_role):
            raise PermissionError(f"Role '{src_role}' is not permitted to {verb} '{dst_role}' under the team topology.")

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
        if not isinstance(env, DiffCapablePort):
            return result
        diff = await env.get_diff()
        if not diff:
            return result
        if len(diff) > WORKTREE_DIFF_MAX_CHARS:
            diff = (
                diff[:WORKTREE_DIFF_KEEP_CHARS]
                + f"\n\n... [{len(diff) - WORKTREE_DIFF_MAX_CHARS} chars truncated] ...\n\n"
                + diff[-WORKTREE_DIFF_KEEP_CHARS:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit a pre-built scheduler event via the event sink.

        Events are built through ``self._events`` (a ``SchedulerEventFactory``)
        so the orchestration vocabulary lives in one place; the sink is a
        required ``EventPublisherPort``, so emission is a single ``emit``.
        """
        await self._event_sink.emit(event)

    async def _safe_emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit an observational lifecycle event without changing scheduler state."""
        try:
            await self.emit_scheduler_event(event)
        except Exception as exc:
            logger.error("scheduler event %s failed: %s", event.type, exc)
