"""Shared scripted sessions for scheduler awaiting tests."""

from __future__ import annotations

import asyncio

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)

class ScriptedSession:
    """run_loop pops the next scripted step; each step mutates state + returns."""

    def __init__(self, role, steps):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.added: list[str] = []
        self._steps = list(steps)
        self.scheduler: Scheduler | None = None

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        step = self._steps.pop(0)
        return await step(self)

def suspend_spawning(specs):
    """Build a step that spawns one child per spec and suspends.

    ``specs`` is a list of (role, task, tool_call_id). Adds a PENDING row per
    spawn referencing the child aid, then transitions to AWAITING_EVENTS.
    """

    async def step(sess: ScriptedSession) -> str:
        for order, (role, task, tcid) in enumerate(specs):
            child_aid = await sess.scheduler.spawn(
                sess.state.aid, role, task, tool_call_id=tcid
            )
            sess.state.pending_events.add(
                PendingRow(
                    tool_call_id=tcid,
                    kind=RowKind.CHILD_AGENT,
                    order=order,
                    ref=child_aid,
                    status=RowStatus.PENDING,
                )
            )
        sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
        sess.state.append_message({"role": "assistant", "content": "delegating"})
        return "delegating"

    return step

def resume_done(final_builder):
    """Build a step that drains a now-complete pending table and finishes."""

    async def step(sess: ScriptedSession) -> str:
        assert sess.state.pending_events.is_complete()
        results = [r.result for r in sorted(sess.state.pending_events.rows.values(), key=lambda r: r.order)]
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        final = final_builder(results)
        sess.state.append_message({"role": "assistant", "content": final})
        return final

    return step

def terminal(result):
    async def step(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": result})
        return result

    return step

class ScriptedFactory:
    """Hands out pre-scripted child sessions in spawn order, assigning aid."""

    def __init__(self, children, scheduler_ref):
        self._children = list(children)
        self._scheduler_ref = scheduler_ref

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        sess = self._children.pop(0)
        sess.state.aid = aid
        sess.scheduler = scheduler
        return sess

def build_scheduler(lead, children):
    captured: list = []

    async def sink(event):
        captured.append(event)

    holder: dict = {}
    factory = ScriptedFactory(children, holder)
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
    )
    holder["scheduler"] = scheduler
    lead.scheduler = scheduler
    scheduler.register_lead(lead)
    return scheduler, captured

def event_types(events, type_):
    return [e for e in events if isinstance(e, SchedulerEvent) and e.type == type_]
