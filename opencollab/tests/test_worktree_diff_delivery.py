"""Worktree-diff delivery to parents.

A finished child whose environment is diff-capable (``DiffCapablePort``) has
its worktree diff appended to the result the scheduler fills into the parent's
pending row, so the parent reasons over the child's actual changes.
"""

from __future__ import annotations

import asyncio

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState


class _DiffEnv:
    def __init__(self, diff: str):
        self._diff = diff

    async def get_diff(self) -> str:
        return self._diff


class _ChildSession:
    def __init__(self, role: str, result: str, env: _DiffEnv):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = env
        self._result = result

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return self._result


class _LeadSession:
    def __init__(self):
        self.agent = type("_Agent", (), {"name": "lead"})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = None

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _Factory:
    def __init__(self, child: _ChildSession):
        self._child = child

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        self._child.state.aid = aid
        return self._child


def test_child_worktree_diff_is_delivered_to_parent_pending_row():
    diff = "diff --git a/f.py b/f.py\n+print('hi')"
    child = _ChildSession("coder", "implemented it", _DiffEnv(diff))
    lead = _LeadSession()
    scheduler = Scheduler(
        session_factory=_Factory(child),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )
    scheduler.register_lead(lead)

    async def scenario() -> None:
        aid = await scheduler.spawn(0, "coder", "implement", tool_call_id="tc-1")
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="tc-1",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
                status=RowStatus.PENDING,
            )
        )
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        await scheduler._tasks[aid]
        resume = scheduler._tasks.get(0)
        if resume is not None:
            await resume

    asyncio.run(scenario())

    row = lead.state.pending_events.rows["tc-1"]
    assert row.status is RowStatus.DONE
    assert row.result == (
        "implemented it\n\n[Changes made in worktree]\n```diff\n" + diff + "\n```"
    )
