"""Team roster and topology-facing scheduler views."""

import pytest
from test_inter_agent_messaging import FakeSession, _build_scheduler, run

from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase
from opencollab.domain.team import Topology


def test_spawn_from_unknown_parent_is_rejected_even_without_topology_rules():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)

    with pytest.raises(ValueError, match="no parent with aid 99"):
        run(scheduler.spawn(99, "coder", "task"))
    assert scheduler.table.get(1) is None


def test_spawn_denied_by_topology_raises_permission_error():
    teammate = FakeSession([], role="coder")
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)
    with pytest.raises(PermissionError, match="not permitted to spawn 'reviewer'"):
        run(scheduler.spawn(0, "reviewer", "review it"))


def test_send_message_denied_by_topology_returns_error():
    teammate = FakeSession([], role="coder")
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)
    reviewer = FakeSession([], role="reviewer")
    reviewer.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=2, parent_aid=0, agent=reviewer.agent, state=reviewer.state)
    )
    scheduler._sessions[2] = reviewer

    result = run(scheduler.send_message(0, 2, "hello", "there"))
    assert "not permitted to message 'reviewer'" in result
    assert reviewer.added == []


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
    assert roster[0]["aid"] == 0
    assert roster[0]["role"] == "lead"
    available = [e for e in roster if e["aid"] is None]
    assert [e["role"] for e in available] == ["analyst", "coder", "reviewer"]
    assert all(e["phase"] == "available" and e["busy"] is False for e in available)
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
    available_roles = [e["role"] for e in roster if e["aid"] is None]
    assert "coder" not in available_roles
    assert available_roles == ["analyst", "reviewer"]
    assert any(e["aid"] == 1 and e["role"] == "coder" for e in roster)
