"""Integration tests for Scheduler topology enforcement + inter-agent messaging."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.team import Topology


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    """Session stand-in: run_loop returns canned results from a queue."""

    def __init__(self, results, role):
        self._results = list(results)
        self.added: list[str] = []
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return self._results.pop(0) if self._results else ""


class FakeFactory:
    def __init__(self, teammate):
        self._teammate = teammate

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None):
        return self._teammate


def _build_scheduler(teammate, topology=None):
    captured: list = []

    async def sink(event):
        captured.append(event)

    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=sink,
        topology=topology,
    )
    lead = FakeSession([], role="lead")
    scheduler.register_lead(lead)
    return scheduler, captured


def _scheduler_events(events):
    return [e for e in events if isinstance(e, SchedulerEvent)]


def test_send_message_reactivates_target_and_returns_reply():
    teammate = FakeSession(["spawn result", "message reply"], role="coder")
    scheduler, events = _build_scheduler(teammate)

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "do the thing")
        reply = await scheduler.send_message(0, aid, "follow-up question")
        return aid, reply

    aid, reply = run(scenario())

    assert reply == "message reply"
    assert "follow-up question" in teammate.added
    assert scheduler.table.get(aid).result == "message reply"

    types = [e.type for e in _scheduler_events(events)]
    assert "agent_message_sent" in types
    assert "agent_message_delivered" in types


def test_send_message_to_self_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 0, "hi"))
    assert "itself" in result


def test_send_message_to_unknown_aid_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 99, "hi"))
    assert "no agent with aid 99" in result


def test_spawn_denied_by_topology_raises_permission_error():
    teammate = FakeSession([], role="coder")
    # lead may only spawn coder, not reviewer.
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)
    with pytest.raises(PermissionError, match="not permitted to spawn 'reviewer'"):
        run(scheduler.spawn(0, "reviewer", "review it"))


def test_send_message_denied_by_topology_returns_error():
    teammate = FakeSession([], role="coder")
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)

    # Manually register a reviewer the lead is not allowed to message.
    reviewer = FakeSession([], role="reviewer")
    reviewer.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=2, parent_aid=0, agent=reviewer.agent, state=reviewer.state)
    )
    scheduler._sessions[2] = reviewer

    result = run(scheduler.send_message(0, 2, "hello"))
    assert "not permitted to message 'reviewer'" in result
    assert reviewer.added == []  # never delivered


def test_team_snapshot_lists_lead_and_spawned():
    teammate = FakeSession(["done"], role="coder")
    scheduler, _ = _build_scheduler(teammate)

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await scheduler._tasks[aid]
        return scheduler.team_snapshot()

    snapshot = run(scenario())
    by_aid = {e["aid"]: e for e in snapshot}
    assert by_aid[0]["role"] == "lead"
    assert by_aid[0]["parent_aid"] is None
    assert by_aid[1]["role"] == "coder"
    assert by_aid[1]["parent_aid"] == 0
    assert by_aid[1]["busy"] is False
