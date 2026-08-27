"""A prebuilt team gives a run an *assigned* topology, and refuses to grow.

Ordinarily the only agent alive when a run starts is agent 0, and every teammate
is created mid-run by a model calling ``spawn_agent``. So the shape of the team —
how many agents, in which roles — is an outcome of the run, decided by the model,
and knowable only afterwards. There is no assigned value to compare an observed
one against, because nothing assigned one.

``prebuild_team`` inverts that. The scheduler seats one agent per role declared in
the team config before the first model call, records that roster and the
topology's edges as ``assigned.topology_nodes`` / ``assigned.topology_edges``, and
from then on refuses ``spawn`` — recording each refused attempt as
``spawn_refused``, which is the observation that a model wanted a role its team
was not given.

The switch is off by default and these tests pin that too: with it off, the
roster, the spawn path, and the trace are exactly what they were.

The tests drive the real :class:`~opencollab.adapters.trace.Tracer` and read the
JSONL back off disk, so a field that never reaches the file fails here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scheduler_awaiting_test_support import ScriptedFactory, ScriptedSession, terminal

from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.scheduler_types import TeamPrebuiltError
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap import build_runtime_context, build_scheduler
from opencollab.bootstrap.team_config import (
    CODER_TOOL_NAMES,
    TESTER_TOOL_NAMES,
    default_team_config,
)

CONFIG = {
    "model": "gpt-4o",
    "provider": "openai",
    "api_key": "test-key",  # pragma: allowlist secret
    "base_url": None,
    "budget": 1_000_000,
}


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path):
    """A committed git repository — the precondition for real worktrees."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _scheduler(tmp_path, *, prebuild_team, use_worktrees=False, workspace=None, **kwargs):
    """A fully wired scheduler over a throwaway workspace, plus its tracer."""
    if workspace is None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "README.md").write_text("hi", encoding="utf-8")
    traces = tmp_path / "traces"
    traces.mkdir(exist_ok=True)
    tracer = Tracer(run_id="prebuild", output_dir=str(traces))
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    ctx.tracer = tracer
    scheduler = build_scheduler(
        ctx,
        use_worktrees=use_worktrees,
        interactive=False,
        auto_save=False,
        prebuild_team=prebuild_team,
        **kwargs,
    )
    return scheduler, tracer


def _roles(scheduler) -> dict[int, str]:
    return {aid: scb.agent.name for aid, scb in sorted(scheduler.table.entries.items())}


# --- the switch, off ---------------------------------------------------------


async def test_with_the_switch_off_nothing_is_seated_and_nothing_is_recorded(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=False)
    try:
        assert await scheduler.ensure_team_prebuilt() == ()
        assert _roles(scheduler) == {0: "analyst"}
        assert sorted(scheduler._sessions) == [0]
        # The configured teammates are still what they always were: names on a
        # roster with no agent behind them.
        assert [entry["phase"] for entry in scheduler.team_roster()] == [
            "idle",
            "available",
            "available",
        ]
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    assert _payloads(tracer.path, "assigned.topology_nodes") == []
    assert _payloads(tracer.path, "assigned.topology_edges") == []


async def test_with_the_switch_off_spawn_still_creates_an_agent():
    lead = ScriptedSession("analyst", [terminal("done")])
    child = ScriptedSession("coder", [terminal("built")])
    scheduler = Scheduler(
        session_factory=ScriptedFactory([child], {}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(_noop_sink),
        roles=("analyst", "coder"),
    )
    lead.scheduler = scheduler
    scheduler.register_lead(lead)

    assert await scheduler.spawn(0, "coder", "implement it") == 1
    assert _roles(scheduler) == {0: "analyst", 1: "coder"}
    await scheduler.cleanup()


async def _noop_sink(event) -> None:
    return None


# --- the switch, on ----------------------------------------------------------


async def test_every_declared_role_is_seated_before_the_first_model_call(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        assert sorted(scheduler._sessions) == [0], "agent 0 alone until prebuild runs"

        assert await scheduler.ensure_team_prebuilt() == (1, 2)

        assert _roles(scheduler) == {0: "analyst", 1: "coder", 2: "tester"}
        assert sorted(scheduler._sessions) == [0, 1, 2]
        # Not one model call has happened: every session is idle, unstepped and
        # has spent nothing. This is what "before the first turn" has to mean.
        assert [scb.state.phase.value for scb in scheduler.table.entries.values()] == [
            "idle",
            "idle",
            "idle",
        ]
        assert [session.step_count for session in scheduler._sessions.values()] == [0, 0, 0]
        assert [session.used_tokens for session in scheduler._sessions.values()] == [0, 0, 0]
        # Nothing is left "available": the configured team is the live team.
        assert [entry["phase"] for entry in scheduler.team_roster()] == ["idle"] * 3
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_a_prebuilt_peer_is_a_child_of_the_init_process_with_no_join_route(tmp_path):
    """``parent_aid`` is 0, and the spawn join path is deliberately empty.

    ``parent_aid=None`` is the scheduler's marker for agent 0, the root — the
    hook bridge reads exactly that to tell "the team stopped" from "a teammate
    finished". A peer must not claim it. But a peer is also not a spawn: nothing
    is blocked on its result, so it gets no ``_spawn_origin`` entry and no
    completion of it is ever routed to a pending row.
    """
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        parents = {aid: scb.parent_aid for aid, scb in scheduler.table.entries.items()}
        assert parents == {0: None, 1: 0, 2: 0}
        assert scheduler._spawn_origin == {}
        assert scheduler._inflight == {}
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_each_prebuilt_teammate_gets_its_own_worktree(tmp_path):
    workspace = _repo(tmp_path / "repo")
    scheduler, tracer = _scheduler(
        tmp_path, prebuild_team=True, use_worktrees=True, workspace=workspace
    )
    try:
        await scheduler.ensure_team_prebuilt()
        spaces = {aid: scheduler._sessions[aid].env.workspace for aid in sorted(scheduler._sessions)}
        # Agent 0 keeps the real repository; nobody else touches it.
        assert spaces[0] == str(workspace.resolve())
        assert spaces[1] != spaces[0] and spaces[2] != spaces[0]
        assert spaces[1] != spaces[2]
        # Each peer's tree is a real, separate checkout of the same repository.
        for aid in (1, 2):
            assert (Path(spaces[aid]) / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_prebuilding_twice_seats_nobody_twice(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        assert await scheduler.ensure_team_prebuilt() == (1, 2)
        assert await scheduler.ensure_team_prebuilt() == ()
        assert sorted(scheduler._sessions) == [0, 1, 2]
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_a_team_the_budget_cannot_seat_fails_at_startup_and_rolls_back(tmp_path):
    """All or nothing, and loudly.

    Each agent reserves up to a quarter of the token pool, so a five-role team
    cannot be seated. A run that quietly seated four of them would record an
    assigned topology its own agents contradict; failing before the first token
    is spent is the cheaper mistake, and it puts the allocation limit in front of
    whoever hits it.
    """
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\n"
        "roles:\n"
        + "".join(
            f"  {name}:\n    prompt: work as the {name}\n    tools: [file_read]\n"
            for name in ("lead", "a", "b", "c", "d")
        ),
        encoding="utf-8",
    )
    scheduler, tracer = _scheduler(
        tmp_path, prebuild_team=True, team_config_path=str(team)
    )
    try:
        with pytest.raises(RuntimeError, match="token budget is fully allocated"):
            await scheduler.ensure_team_prebuilt()
        # Rolled back to the init process alone — no half-seated team is left
        # behind, and no budget stays reserved for an agent that never existed.
        assert sorted(scheduler._sessions) == [0]
        assert sorted(scheduler.table.entries) == [0]
        assert sorted(scheduler._turn_lease) == [0]
    finally:
        tracer.close()
        await scheduler.cleanup()


# --- spawn, refused and recorded --------------------------------------------


def _spawn_tool(scheduler):
    tool = next(t for t in scheduler.lead_session.agent.tools if t.name == "spawn_agent")
    runtime = ToolRuntime(
        environment=scheduler.lead_session.env,
        safety_policy=None,
        permission_policy=None,
        aid=0,
        tool_call_id="call_1",
    )
    return tool, runtime


async def test_the_spawn_tool_survives_but_refuses_and_names_the_live_roster(tmp_path):
    """The tool is left in the tool set on purpose.

    Removing ``spawn_agent`` would remove the observation: a model that never
    gets to ask cannot be seen wanting a role its team does not have. So the ask
    still reaches the scheduler, and the answer names the teammates it should
    have messaged instead.
    """
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        assert "spawn_agent" in {t.name for t in scheduler.lead_session.agent.tools}

        tool, runtime = _spawn_tool(scheduler)
        answer = await tool.execute_with_runtime(
            {"role": "reviewer", "task": "check the patch", "context": "ctx"}, runtime
        )

        assert isinstance(answer, str)
        assert answer.startswith("Not spawned: 'reviewer' is not a role on this team.")
        assert "analyst (aid 0), coder (aid 1), tester (aid 2)" in answer
        assert "send_message" in answer
        # Refused means refused: no agent, no aid burned, no worktree taken.
        assert sorted(scheduler._sessions) == [0, 1, 2]
        assert sorted(scheduler.table.entries) == [0, 1, 2]
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_a_refused_spawn_records_who_wanted_which_role_and_whether_it_existed(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        tool, runtime = _spawn_tool(scheduler)
        # One role the team was never given, and one it already has. Both are
        # refused, and the record has to tell them apart.
        await tool.execute_with_runtime(
            {"role": "reviewer", "task": "check the patch", "context": "abc"}, runtime
        )
        await tool.execute_with_runtime({"role": "coder", "task": "implement it"}, runtime)
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    roster = [
        {"aid": 0, "role": "analyst"},
        {"aid": 1, "role": "coder"},
        {"aid": 2, "role": "tester"},
    ]
    assert _payloads(tracer.path, "spawn_refused") == [
        {
            "reason": "team_prebuilt",
            "requester_aid": 0,
            "requester_role": "analyst",
            "requested_role": "reviewer",
            "requested_role_declared": False,
            # The closed default topology would have refused this edge anyway;
            # recording it keeps the two causes from being conflated later.
            "topology_allowed": False,
            "declared_roles": ["analyst", "coder", "tester"],
            "live_roster": roster,
            "task": "check the patch",
            "task_chars": 15,
            "context_chars": 3,
        },
        {
            "reason": "team_prebuilt",
            "requester_aid": 0,
            "requester_role": "analyst",
            "requested_role": "coder",
            "requested_role_declared": True,
            "topology_allowed": True,
            "declared_roles": ["analyst", "coder", "tester"],
            "live_roster": roster,
            "task": "implement it",
            "task_chars": 12,
            "context_chars": 0,
        },
    ]


async def test_a_long_delegation_is_still_measurable_as_long(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        with pytest.raises(TeamPrebuiltError):
            await scheduler.spawn(0, "reviewer", "x" * 5000)
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    payload = _payloads(tracer.path, "spawn_refused")[0]
    assert payload["task"] == "x" * 1000
    assert payload["task_chars"] == 5000


# --- the assigned topology ---------------------------------------------------


async def test_the_recorded_edges_are_the_team_config_edges(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    declared = default_team_config().topology
    expected = sorted(
        ({"from_role": source, "to_role": destination}
         for source, destinations in declared.edges.items()
         for destination in destinations),
        key=lambda edge: (edge["from_role"], edge["to_role"]),
    )
    assert _payloads(tracer.path, "assigned.topology_edges") == [
        {
            "allow_all": False,
            "declared_roles": ["analyst", "coder", "tester"],
            "edges": expected,
        }
    ]
    # Spelled out, so a change to the built-in team has to be made here too.
    assert expected == [
        {"from_role": "analyst", "to_role": "coder"},
        {"from_role": "analyst", "to_role": "tester"},
    ]


async def test_the_recorded_nodes_are_the_agents_that_were_actually_seated(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.ensure_team_prebuilt()
        workspaces = {
            aid: scheduler._sessions[aid].env.workspace for aid in sorted(scheduler._sessions)
        }
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    (payload,) = _payloads(tracer.path, "assigned.topology_nodes")
    assert payload["entry_role"] == "analyst"
    assert payload["declared_roles"] == ["analyst", "coder", "tester"]
    assert payload["nodes"] == [
        {
            "aid": 0,
            "role": "analyst",
            "entry": True,
            # Headless, so the analyst's ask_user is dropped by the registry.
            "tools": ["file_read", "grep", "spawn_agent", "use_skill"],
            "permission_mode": "auto",
            "workspace": workspaces[0],
            # use_worktrees=False: everyone shares one directory, and the record
            # says so rather than implying an isolation that is not there.
            "workspace_isolated": False,
        },
        {
            "aid": 1,
            "role": "coder",
            "entry": False,
            "tools": sorted(CODER_TOOL_NAMES),
            "permission_mode": "auto",
            "workspace": workspaces[1],
            "workspace_isolated": False,
        },
        {
            "aid": 2,
            "role": "tester",
            "entry": False,
            "tools": sorted(TESTER_TOOL_NAMES),
            "permission_mode": "auto",
            "workspace": workspaces[2],
            "workspace_isolated": False,
        },
    ]
