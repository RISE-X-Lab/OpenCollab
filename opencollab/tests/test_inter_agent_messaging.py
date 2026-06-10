"""Integration tests for Scheduler topology enforcement + inter-agent messaging."""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
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


class PersistingFakeSession(FakeSession):
    def __init__(self, results, role, auto_save_path):
        super().__init__(results, role)
        self.auto_save_path = str(auto_save_path)

    def save(self, path: str) -> None:
        obj = {"messages": self.state.enriched_messages()}
        if self.state.pending_user_messages:
            obj["pending_messages"] = self.state.enriched_pending_user_messages()
        with open(path, "w") as f:
            json.dump(obj, f)


class FakeFactory:
    def __init__(self, teammate):
        self._teammate = teammate

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        return self._teammate


def _build_scheduler(teammate, topology=None, roles=()):
    captured: list = []

    async def sink(event):
        captured.append(event)

    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        topology=topology,
        roles=roles,
    )
    lead = FakeSession([], role="lead")
    scheduler.register_lead(lead)
    return scheduler, captured


def _scheduler_events(events):
    return [e for e in events if isinstance(e, SchedulerEvent)]


async def _wait_agent_idle(scheduler, aid: int) -> None:
    for _ in range(5):
        task = scheduler._tasks.get(aid)
        if task is None:
            return
        await task
        if scheduler._tasks.get(aid) is task and not scheduler._message_inbox.get(aid):
            return


def test_send_message_queues_xml_and_returns_ack():
    teammate = FakeSession(["spawn result", "message result"], role="coder")
    scheduler, events = _build_scheduler(teammate)

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "do the thing")
        ack = await scheduler.send_message(0, aid, "follow up", "check <this> & report")
        await _wait_agent_idle(scheduler, aid)
        return aid, ack

    aid, ack = run(scenario())

    assert ack == f"Message queued to aid {aid}."
    assert teammate.added[-1] == (
        '<teammate-message teammate_id="A0" summary="follow up">\n'
        "check &lt;this&gt; &amp; report\n"
        "</teammate-message>"
    )
    assert scheduler.table.get(aid).result == "message result"

    types = [e.type for e in _scheduler_events(events)]
    assert "agent_message_sent" in types
    assert "agent_message_delivered" in types


def test_send_message_to_idle_target_schedules_background_run():
    teammate = FakeSession(["message result"], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    async def scenario():
        ack = await scheduler.send_message(0, 1, "hello", "please review")
        await scheduler._tasks[1]
        return ack

    ack = run(scenario())

    assert ack == "Message queued to aid 1."
    assert len(teammate.added) == 1
    assert scheduler.table.get(1).result == "message result"


def test_send_message_to_busy_target_autosaves_pending_xml(tmp_path):
    path = tmp_path / "agent_1_coder.json"
    teammate = PersistingFakeSession([], role="coder", auto_save_path=path)
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    result = run(scheduler.send_message(0, 1, "follow up", "check <this> & report"))

    assert result == "Message queued to aid 1."
    assert teammate.added == []
    with open(path) as f:
        saved = json.load(f)
    assert saved["messages"] == []
    assert saved["pending_messages"][0]["content"] == (
        '<teammate-message teammate_id="A0" summary="follow up">\n'
        "check &lt;this&gt; &amp; report\n"
        "</teammate-message>"
    )


def test_send_message_to_self_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 0, "hi", "there"))
    assert "itself" in result


def test_send_message_to_unknown_aid_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 99, "hi", "there"))
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

    result = run(scheduler.send_message(0, 2, "hello", "there"))
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


def test_team_roster_surfaces_configured_unspawned_roles():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(
        teammate, roles=("lead", "analyst", "coder", "reviewer")
    )

    roster = scheduler.team_roster()
    # Lead is live (aid 0); the rest are configured-but-unspawned.
    assert roster[0]["aid"] == 0
    assert roster[0]["role"] == "lead"
    available = [e for e in roster if e["aid"] is None]
    assert [e["role"] for e in available] == ["analyst", "coder", "reviewer"]
    assert all(e["phase"] == "available" and e["busy"] is False for e in available)
    # team_snapshot stays live-only.
    assert [e["aid"] for e in scheduler.team_snapshot()] == [0]


def test_team_roster_drops_role_once_spawned():
    teammate = FakeSession(["done"], role="coder")
    scheduler, _ = _build_scheduler(
        teammate, roles=("lead", "analyst", "coder", "reviewer")
    )

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await scheduler._tasks[aid]
        return scheduler.team_roster()

    roster = run(scenario())
    # coder is now live (by aid), so it is not also listed as available.
    available_roles = [e["role"] for e in roster if e["aid"] is None]
    assert "coder" not in available_roles
    assert available_roles == ["analyst", "reviewer"]
    assert any(e["aid"] == 1 and e["role"] == "coder" for e in roster)
