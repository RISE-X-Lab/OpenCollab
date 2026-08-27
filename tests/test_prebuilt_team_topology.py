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

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.scheduler_types import TeamPrebuiltError
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap import build_runtime_context, build_scheduler
from opencollab.bootstrap.context_builder import SpawnConfig
from opencollab.bootstrap.session_factory import DefaultSessionFactory
from opencollab.bootstrap.team_config import (
    ANALYST_TOOL_NAMES,
    CODER_TOOL_NAMES,
    TESTER_TOOL_NAMES,
    TeamConfig,
    default_team_config,
    load_team_config,
)
from opencollab.domain.scheduler import PER_AGENT_BUDGET_SHARE, per_agent_cap

CONFIG = {
    "model": "gpt-4o",
    "provider": "openai",
    "api_key": "test-key",  # pragma: allowlist secret
    "base_url": None,
    "budget": 1_000_000,
}
# The team the handoff experiment runs: three peers, two of them carrying bash
# so they can hand a commit to each other over git.
HANDOFF_TEAM_FILE = (
    Path(__file__).resolve().parents[1] / "configs" / "team.handoff.experiment.yaml"
)


SCHEDULER_STANDIN = object()


def _spawn_config(ctx) -> SpawnConfig:
    return SpawnConfig(
        model=CONFIG["model"],
        provider=CONFIG["provider"],
        api_key=CONFIG["api_key"],
        base_url=CONFIG["base_url"],
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(ctx.event_sink),
        permission_policy=None,
    )


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


def _prebuildable_default() -> TeamConfig:
    """The built-in team with the one change prebuild mode forces on it.

    A prebuilt team refuses ``spawn_agent``, so ``message_agent`` is the only
    tool left that can walk a topology edge — and ``build_scheduler`` refuses a
    prebuilt team whose Analyst is given two outgoing edges and nothing to walk
    them with. The built-in team is exactly that team: it delegates by spawning
    and collects results on the join path, which is legitimate everywhere except
    here.

    Everything else is the shipped configuration verbatim — the same three
    roles, the same edges, the Coder's and Tester's tool bundles untouched — so
    the assertions below still pin what OpenCollab ships.
    """
    team = default_team_config()
    roles = dict(team.roles)
    roles["analyst"] = roles["analyst"].model_copy(
        update={"tools": sorted({*ANALYST_TOOL_NAMES, "message_agent"})}
    )
    return TeamConfig(roles=roles, topology=team.topology, entry=team.entry)


def _scheduler(
    tmp_path,
    *,
    prebuild_team,
    use_worktrees=False,
    workspace=None,
    interactive=False,
    **kwargs,
):
    """A fully wired scheduler over a throwaway workspace, plus its tracer."""
    if prebuild_team and not {"team_config_path", "resolved_team_config"} & set(kwargs):
        kwargs["resolved_team_config"] = _prebuildable_default()
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
        interactive=interactive,
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


async def test_a_five_role_team_seats_and_every_seat_gets_the_same_cap(tmp_path):
    """Team size is not a budget question any more.

    The pool used to be divided by reservation: each agent took up to a quarter
    of it when it was created, so agent 0 plus three teammates emptied it and a
    five-role team could not be seated at all. Nothing is reserved at seating
    now — every agent draws from the one shared pool and is bounded by
    ``per_agent_cap`` — so the declared roster decides how many agents there are
    and the budget decides only how much each may spend.

    The caps are equal, agent 0 included: it is seated under the same rule as
    every teammate, and the session it was built with carries that same number
    (which is what the model is told each turn).
    """
    team = tmp_path / "team.yaml"
    names = ("lead", "a", "b", "c", "d")
    team.write_text(
        "entry: lead\n"
        "roles:\n"
        + "".join(
            f"  {name}:\n    prompt: work as the {name}\n    tools: [file_read]\n"
            for name in names
        ),
        encoding="utf-8",
    )
    scheduler, tracer = _scheduler(
        tmp_path, prebuild_team=True, team_config_path=str(team)
    )
    try:
        assert await scheduler.ensure_team_prebuilt() == (1, 2, 3, 4)
        assert sorted(scheduler._sessions) == [0, 1, 2, 3, 4]
        assert list(_roles(scheduler).values()) == list(names)

        total = scheduler._max_budget_tokens
        cap = per_agent_cap(total, len(names))
        assert cap == int(total * PER_AGENT_BUDGET_SHARE / len(names))
        assert scheduler._per_agent_cap() == cap
        # One cap for the whole team — agent 0 has no larger allowance.
        assert [
            scheduler._sessions[aid].max_budget_tokens for aid in sorted(scheduler._sessions)
        ] == [cap] * len(names)
        # Seating five agents committed nothing: the pool is shared, so an agent
        # that has not spent freezes nothing for the ones that have.
        assert scheduler.allocated_tokens == 0
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
            # ``message_agent`` is the one addition ``_prebuildable_default``
            # makes: without it the Analyst's two edges are unwalkable and the
            # team is refused before it is seated.
            "tools": [
                "file_read",
                "grep",
                "message_agent",
                "spawn_agent",
                "use_skill",
            ],
            "permission_mode": "auto",
            "workspace": workspaces[0],
            # use_worktrees=False: everyone shares one directory, and the record
            # says so rather than implying an isolation that is not there.
            "workspace_isolated": False,
            # The Analyst was never given ``bash`` — a different fact from
            # "carries bash and would be refused", which is what the Coder and
            # Tester record here.
            "shell": "absent",
        },
        {
            "aid": 1,
            "role": "coder",
            "entry": False,
            "tools": sorted(CODER_TOOL_NAMES),
            "permission_mode": "auto",
            "workspace": workspaces[1],
            "workspace_isolated": False,
            # ``interactive=False`` above, so nobody in this run may open a
            # shell the OS does not sandbox — and a shared local directory is
            # not one. The peers record the same answer agent 0 would: the
            # switch is run-wide, not per-agent.
            "shell": "sandbox_required",
        },
        {
            "aid": 2,
            "role": "tester",
            "entry": False,
            "tools": sorted(TESTER_TOOL_NAMES),
            "permission_mode": "auto",
            "workspace": workspaces[2],
            "workspace_isolated": False,
            # The shipped Tester runs tests, not shell commands.
            "shell": "absent",
        },
    ]


# --- the capabilities a seat comes with --------------------------------------


def _bash(session):
    for tool in session.agent.tools:
        if tool.name == "bash":
            return tool
    raise AssertionError(f"{session.agent.name} was built without bash")


async def test_a_seated_peer_gets_the_entry_agent_s_shell(tmp_path):
    """The whole point of the split: one run, one answer about the shell.

    A peer is a non-entry role, so it is built with no ``ask_user``. While that
    one fact also decided the shell, every peer was seated with a ``bash`` that
    refused to run in the worktree it was given, while agent 0 — same run, same
    machine, same repository — had a working one. A team measured against a lone
    agent under that arrangement is not measuring the team.
    """
    team = load_team_config(path=str(HANDOFF_TEAM_FILE))
    scheduler, tracer = _scheduler(
        tmp_path,
        prebuild_team=True,
        use_worktrees=True,
        workspace=_repo(tmp_path / "repo"),
        interactive=True,
        resolved_team_config=team,
    )
    try:
        await scheduler.ensure_team_prebuilt()
        seats = {
            session.agent.name: _bash(session).require_process_isolation
            for aid, session in sorted(scheduler._sessions.items())
            if any(tool.name == "bash" for tool in session.agent.tools)
        }
        entry = scheduler._sessions[0]
    finally:
        tracer.close()
        await scheduler.cleanup()

    # The Analyst carries no bash, so agent 0's shell answer is read off the
    # switch itself rather than off a tool it does not have.
    assert "ask_user" in {tool.name for tool in entry.agent.tools}
    assert seats == {"coder": False, "tester": False}


async def test_a_child_the_model_spawns_is_not_a_seat_and_keeps_the_hard_default(
    tmp_path,
):
    """The product default, unchanged: a mid-run child still needs a sandbox.

    A prebuilt roster is declared in a file by a human before the run starts. A
    ``spawn_agent`` child is not declared anywhere — a running model asked for
    it — so it does not inherit agent 0's shell, whatever agent 0 has.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    factory = DefaultSessionFactory(
        _spawn_config(ctx),
        team_cfg=load_team_config(path=str(HANDOFF_TEAM_FILE)),
        lead_workspace=str(workspace),
        interactive=True,
    )
    env = LocalEnvironment(str(workspace))
    # A stand-in scheduler: the coordination tools only store the reference.
    ad_hoc = factory.build_spawn_session(
        role="coder", env=env, budget=10_000, aid=1, scheduler=SCHEDULER_STANDIN
    )
    assert _bash(ad_hoc).require_process_isolation is True

    seated = DefaultSessionFactory(
        _spawn_config(ctx),
        team_cfg=load_team_config(path=str(HANDOFF_TEAM_FILE)),
        lead_workspace=str(workspace),
        interactive=True,
        prebuilt_roster=True,
    ).build_spawn_session(
        role="coder", env=env, budget=10_000, aid=1, scheduler=SCHEDULER_STANDIN
    )
    assert _bash(seated).require_process_isolation is False


async def test_no_seat_gets_a_shell_the_entry_agent_would_not_have(tmp_path):
    """The other direction of the same rule, and the one that keeps it honest.

    ``interactive=False`` is a run with nobody to show a risky command to, so
    agent 0 may not open an unsandboxed shell — and neither may its peers. A
    fix that only ever *adds* capability would pass the test above and still
    leave the two sides on different answers.
    """
    scheduler, tracer = _scheduler(
        tmp_path,
        prebuild_team=True,
        use_worktrees=True,
        workspace=_repo(tmp_path / "repo"),
        interactive=False,
        resolved_team_config=load_team_config(path=str(HANDOFF_TEAM_FILE)),
    )
    try:
        await scheduler.ensure_team_prebuilt()
        seats = {
            session.agent.name: _bash(session).require_process_isolation
            for session in scheduler._sessions.values()
            if any(tool.name == "bash" for tool in session.agent.tools)
        }
    finally:
        tracer.close()
        await scheduler.cleanup()

    assert seats == {"coder": True, "tester": True}


# --- a declared edge nobody can walk -----------------------------------------


def _mute_team(tmp_path, *, messaging: bool) -> Path:
    """A two-role team whose Coder is told to answer the Analyst.

    With ``messaging`` off, the Coder holds the edge ``coder -> analyst`` and no
    tool that can carry anything along it. That is the defect this section is
    about: the topology promises a return channel the agent cannot use.
    """
    coder_tools = "[file_read, message_agent]" if messaging else "[file_read]"
    team = tmp_path / f"team-{'talking' if messaging else 'mute'}.yaml"
    team.write_text(
        "entry: analyst\n"
        "roles:\n"
        "  analyst:\n"
        "    prompt: plan the work\n"
        "    tools: [file_read, message_agent]\n"
        "  coder:\n"
        "    prompt: do the work\n"
        f"    tools: {coder_tools}\n"
        "topology:\n"
        "  analyst: [coder]\n"
        "  coder: [analyst]\n",
        encoding="utf-8",
    )
    return team


async def test_a_prebuilt_role_with_an_edge_and_no_way_to_walk_it_is_refused(tmp_path):
    """Naming the role and the edge, because "invalid config" is not actionable.

    A prebuilt team refuses ``spawn_agent``, so ``message_agent`` is the only
    channel between two seated agents. A role given an outgoing edge without it
    is seated mute: the run would start, record the edge as assigned, and end
    with a transcript in which that edge was never used — which reads as a
    finding about the model rather than about the config.
    """
    with pytest.raises(ValueError) as raised:
        _scheduler(
            tmp_path,
            prebuild_team=True,
            team_config_path=str(_mute_team(tmp_path, messaging=False)),
        )
    message = str(raised.value)
    assert "prebuild_team: this team declares edges that no agent can walk." in message
    assert "role 'coder' may address analyst" in message
    assert "unwalkable edges: coder -> analyst" in message
    assert "Add 'message_agent' to the `tools:` list of 'coder'" in message
    # The Analyst holds the same kind of edge and the tool to walk it, so it is
    # not named: the report is the roles that need changing, not every role.
    assert "role 'analyst'" not in message


async def test_the_same_team_starts_once_the_missing_tool_is_given_back(tmp_path):
    scheduler, tracer = _scheduler(
        tmp_path,
        prebuild_team=True,
        team_config_path=str(_mute_team(tmp_path, messaging=True)),
    )
    try:
        assert await scheduler.ensure_team_prebuilt() == (1,)
        assert _roles(scheduler) == {0: "analyst", 1: "coder"}
    finally:
        tracer.close()
        await scheduler.cleanup()


async def test_the_refusal_lands_before_agent_0_exists(tmp_path):
    """Same bargain the unfillable-seat failure already makes: fail before a
    token is spent, not after a run has produced an assigned topology it could
    never have honoured. Nothing is built here — not even the run folder that
    every transcript is written into."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    with pytest.raises(ValueError):
        build_scheduler(
            ctx,
            use_worktrees=False,
            interactive=False,
            auto_save=True,
            prebuild_team=True,
            team_config_path=str(_mute_team(tmp_path, messaging=False)),
        )
    assert not (workspace / ".opencollab").exists()


async def test_the_shipped_team_still_starts_with_the_switch_off(tmp_path):
    """The product default must not be caught by this.

    The built-in Analyst has two outgoing edges and no ``message_agent`` — the
    exact shape refused above. With ``prebuild_team`` off it is not a defect:
    ``spawn_agent`` walks those edges and the join path carries the results
    back. The guard is behind the switch precisely so this keeps working.
    """
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=False)
    try:
        shipped = default_team_config()
        assert "message_agent" not in shipped.roles["analyst"].tools
        assert sorted(shipped.topology.edges["analyst"]) == ["coder", "tester"]
        assert _roles(scheduler) == {0: "analyst"}
        assert await scheduler.spawn(0, "coder", "implement it") == 1
    finally:
        tracer.close()
        await scheduler.cleanup()
