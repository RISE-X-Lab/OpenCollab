"""Scheduler-side tests for event-driven suspend/resume.

A ``ScriptedSession`` stands in for a real Session, honoring the contract the
scheduler relies on: ``run_loop`` returns with the session either suspended on
``AWAITING_EVENTS`` (after recording pending rows) or terminal. On resume the
scheduler has filled the pending table; the scripted session drains it and
finishes. This exercises the scheduler's origin map, ``_wake``, ``_quiescent``,
and ``run`` quiescence without standing up an LLM.
"""

from __future__ import annotations

import asyncio

import pytest
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.pending import PendingRow, PendingRowError, RowKind, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
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


def test_single_deferred_spawn_round_trip():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [terminal("child output")])
    scheduler, events = build_scheduler(lead, [child])

    result = run(scheduler.run("please delegate"))

    # The lead reasoned over the child's result in the SAME turn — the gap closed.
    assert result == "final: child output"
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert len(event_types(events, "agent_resumed")) == 1
    assert len(event_types(events, "agent_completed")) == 2  # child + lead


def test_multiple_parallel_children_resume_exactly_once():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning(
                [("coder", "a", "tc-1"), ("coder", "b", "tc-2"), ("coder", "c", "tc-3")]
            ),
            resume_done(lambda results: "merged: " + ",".join(results)),
        ],
    )
    children = [
        ScriptedSession("coder", [terminal("ra")]),
        ScriptedSession("coder", [terminal("rb")]),
        ScriptedSession("coder", [terminal("rc")]),
    ]
    scheduler, events = build_scheduler(lead, children)

    result = run(scheduler.run("delegate three"))

    assert result == "merged: ra,rb,rc"
    # The last child to finish triggers exactly one resume — no double-wake.
    assert len(event_types(events, "agent_resumed")) == 1
    assert lead.state.pending_events.is_empty()


def test_nested_delegation_resumes_up_the_tree():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("manager", "plan", "L1")]),
            resume_done(lambda results: f"lead<{results[0]}>"),
        ],
    )
    manager = ScriptedSession(
        "manager",
        [
            suspend_spawning([("coder", "impl", "M1")]),
            resume_done(lambda results: f"mgr<{results[0]}>"),
        ],
    )
    grandchild = ScriptedSession("coder", [terminal("leaf")])
    scheduler, events = build_scheduler(lead, [manager, grandchild])

    result = run(scheduler.run("deep delegate"))

    assert result == "lead<mgr<leaf>>"
    assert len(event_types(events, "agent_resumed")) == 2  # manager, then lead


def test_wake_unknown_tool_call_id_raises():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )

    with pytest.raises(PendingRowError):
        run(scheduler._wake(0, "tc-unknown", "x", RowStatus.DONE))


def test_misrouted_completion_emits_failure_not_silent():
    lead = ScriptedSession("lead", [])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )
    # Register a bogus origin pointing at a tool_call_id that doesn't exist.
    scheduler._spawn_origin[99] = (0, "tc-bogus")

    run(scheduler._deliver_to_parent(99, "result", RowStatus.DONE))

    assert len(event_types(events, "agent_failed")) == 1


def test_send_message_queues_when_target_awaiting_events():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession("coder", [])
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    result = run(scheduler.send_message(0, 1, "question", "are you there?"))

    assert result == "Message queued to aid 1."
    assert target.added == []  # never delivered
    assert scheduler._message_inbox[1][0].content == "are you there?"


def test_queued_message_delivers_after_target_awaiting_events_resumes():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession(
        "coder",
        [
            resume_done(lambda results: f"finished {results[0]}"),
            terminal("message handled"),
        ],
    )
    target.state.aid = 1
    target.scheduler = scheduler
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    target.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=7)
    )
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    async def scenario():
        await scheduler.send_message(0, 1, "follow up", "are you there?")
        await scheduler._wake(1, "tc-1", "child done", RowStatus.DONE)
        for _ in range(3):
            task = scheduler._tasks.get(1)
            if task is None:
                break
            await task
            if scheduler._tasks.get(1) is task:
                break

    run(scenario())

    assert target.added == [
        '<teammate-message teammate_id="A0" summary="follow up">\n'
        "are you there?\n"
        "</teammate-message>"
    ]
    assert scheduler.table.get(1).result == "message handled"


def test_child_message_to_suspended_parent_does_not_stall_turn():
    async def child_reports_progress(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(
            sess.state.aid,
            0,
            "progress",
            "analysis started",
        )
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "child final"})
        return "child final"

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("analyst", "investigate", "tc-1")]),
            resume_done(lambda results: f"lead saw {results[0]}"),
            terminal("progress message handled"),
        ],
    )
    child = ScriptedSession("analyst", [child_reports_progress])
    scheduler, events = build_scheduler(lead, [child])

    result = run(asyncio.wait_for(scheduler.run("go"), timeout=1))

    assert result == "progress message handled"
    assert scheduler._message_inbox.get(0) == []
    assert lead.added[-1] == (
        '<teammate-message teammate_id="A1" summary="progress">\n'
        "analysis started\n"
        "</teammate-message>"
    )
    assert len(event_types(events, "agent_message_sent")) == 1
    assert len(event_types(events, "agent_message_delivered")) == 1
