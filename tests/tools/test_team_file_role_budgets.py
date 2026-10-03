"""A team file sets each role's token allowance, and the allowances are independent.

Until now a team's tokens were one number handed to ``client.team(budget=)``
and divided by rule (``per_agent_cap``); the team file could not say what any
role was allowed to spend. A file may now declare ``budget: {tokens: N}`` as
every role's allowance and override it per role. Each agent is then held to its
own allowance alone: what one teammate spends, overshoots included, never
reduces what another may spend. The team's total is the sum of the allowances.

A file that declares no budget runs exactly as before; that regression is held
by ``test_declared_budget_rule.py``, which this change leaves untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap.team_config import load_team_config
from opencollab.domain.session import SessionPhase, SessionState

ROLES = ("lead", "coder", "tester")
ALLOWANCES = {"lead": 300_000, "coder": 200_000, "tester": 100_000}


def run(coro):
    return asyncio.run(coro)


def _write(tmp_path, body: str):
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    prompt: hi\n"
        "  coder:\n    prompt: hi\n"
        + body,
        encoding="utf-8",
    )
    return path


# --- Declaration and validation ------------------------------------------------


def test_no_budget_declared_leaves_the_team_on_the_shared_rule(tmp_path) -> None:
    team = load_team_config(path=str(_write(tmp_path, "")))
    assert team.role_budgets == {}


def test_a_team_level_allowance_applies_to_every_role(tmp_path) -> None:
    team = load_team_config(path=str(_write(tmp_path, "budget:\n  tokens: 2000000\n")))
    assert team.role_budgets == {"lead": 2_000_000, "coder": 2_000_000}


def test_a_role_may_override_the_team_allowance(tmp_path) -> None:
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: lead\nbudget: {tokens: 2000000}\nroles:\n"
        "  lead:\n    prompt: hi\n    budget: {tokens: 3000000}\n"
        "  coder:\n    prompt: hi\n",
        encoding="utf-8",
    )
    assert load_team_config(path=str(path)).role_budgets == {"lead": 3_000_000, "coder": 2_000_000}


@pytest.mark.parametrize(
    "body",
    [
        "budget: {tokens: 0}\n",
        "budget: {tokens: true}\n",
        "budget: {tokens: 100, share: 1}\n",
        "budget: 100\n",
    ],
    ids=["zero", "boolean", "unknown-key", "bare-number"],
)
def test_a_malformed_allowance_is_refused_at_load(tmp_path, body) -> None:
    with pytest.raises(ValueError):
        load_team_config(path=str(_write(tmp_path, body)))


def test_every_role_needs_an_allowance_once_any_has_one(tmp_path) -> None:
    """A role left without one would silently fall back to a different rule."""
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    prompt: hi\n    budget: {tokens: 3000000}\n"
        "  coder:\n    prompt: hi\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="coder"):
        load_team_config(path=str(path))


# --- The scheduler ---------------------------------------------------------------


class FakeSession:
    def __init__(self, role: str, budget: int):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.max_budget_tokens = budget
        self.used_tokens = 0

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return "done"


class RecordingFactory:
    def __init__(self) -> None:
        self.lead_budget: int | None = None
        self.spawn_budgets: dict[str, int] = {}

    def create_lead_session(self, *, scheduler, launch, budget, aid=0):
        self.lead_budget = budget
        return FakeSession("lead", budget)

    def build_spawn_session(self, *, role, env, budget, aid=-1, **kwargs):
        self.spawn_budgets[role] = budget
        session = FakeSession(role, budget)
        session.state.aid = aid
        return session


class _NoWorktrees:
    async def acquire(self, role):
        return None

    async def release(self):
        return None


class RecordingTracer:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict[str, Any]]] = []

    def log_step(self, *, step_type: str, payload: dict[str, Any]) -> None:
        self.steps.append((step_type, payload))


def _scheduler(*, tracer=None, **overrides):
    factory = RecordingFactory()
    kwargs: dict[str, Any] = dict(
        session_factory=factory,
        worktree_pool=_NoWorktrees(),
        event_sink=EventBus(None),
        tracer=tracer,
        max_budget_tokens=sum(ALLOWANCES.values()),
        roles=ROLES,
        prebuild_team=True,
        role_budgets=ALLOWANCES,
        entry_role="lead",
    )
    kwargs.update(overrides)
    scheduler = Scheduler(**kwargs)
    lead = factory.create_lead_session(
        scheduler=scheduler, launch=None, budget=scheduler._entry_start_budget()
    )
    scheduler.register_lead(lead)
    return scheduler, factory


def _spend(scheduler: Scheduler, aid: int, tokens: int) -> None:
    scheduler.table.get(aid).state.used_tokens = tokens


def test_role_allowances_need_a_declared_roster() -> None:
    with pytest.raises(ValueError, match="prebuild"):
        _scheduler(prebuild_team=False)


def test_role_allowances_must_cover_every_declared_role() -> None:
    with pytest.raises(ValueError, match="tester"):
        _scheduler(role_budgets={"lead": 1, "coder": 1})


def test_each_seat_is_built_with_its_roles_allowance() -> None:
    async def scenario():
        scheduler, factory = _scheduler()
        await scheduler.ensure_team_prebuilt()
        assert factory.lead_budget == ALLOWANCES["lead"]
        assert factory.spawn_budgets == {"coder": 200_000, "tester": 100_000}

    run(scenario())


def test_one_agents_overshoot_never_reduces_anothers_allowance() -> None:
    async def scenario():
        scheduler, _ = _scheduler()
        await scheduler.ensure_team_prebuilt()
        # The lead overshoots its own allowance inside one turn, and the coder
        # has spent nearly all of its own.
        _spend(scheduler, 0, ALLOWANCES["lead"] + 250_000)
        _spend(scheduler, 1, 150_000)
        # The pool alone would lend the tester nothing; its allowance is intact.
        assert scheduler.used_tokens >= sum(ALLOWANCES.values())
        assert scheduler._reserve_turn_lease(2) == ALLOWANCES["tester"]
        assert scheduler._reserve_turn_lease(1) == 50_000
        assert scheduler._reserve_turn_lease(0) == 0
        assert scheduler.budget_exhausted is False

    run(scenario())


def test_the_team_is_exhausted_once_every_seat_has_spent_its_allowance() -> None:
    async def scenario():
        scheduler, _ = _scheduler()
        await scheduler.ensure_team_prebuilt()
        for aid, role in enumerate(ROLES):
            _spend(scheduler, aid, ALLOWANCES[role])
        assert scheduler.budget_exhausted is True

    run(scenario())


def test_each_seated_node_records_its_allowance() -> None:
    async def scenario():
        tracer = RecordingTracer()
        scheduler, _ = _scheduler(tracer=tracer)
        await scheduler.ensure_team_prebuilt()
        nodes = next(p for name, p in tracer.steps if name == "assigned.topology_nodes")
        assert nodes["budget_source"] == "team_file"
        assert {n["role"]: n["token_allowance"] for n in nodes["nodes"]} == ALLOWANCES

    run(scenario())


# --- Wiring ----------------------------------------------------------------------

_BUDGETED_TEAM = (
    "entry: lead\nbudget: {tokens: 2000000}\nroles:\n"
    "  lead:\n    prompt: hi\n    tools: [message_agent]\n    budget: {tokens: 3000000}\n"
    "  coder:\n    prompt: hi\n    tools: [message_agent]\n"
    "topology:\n  lead: [coder]\n  coder: [lead]\n"
)


def test_build_scheduler_takes_the_files_allowances(tmp_path) -> None:
    from opencollab.bootstrap import build_runtime_context, build_scheduler
    from tests.support.prebuilt_team_test_support import CONFIG

    team_file = tmp_path / "team.yaml"
    team_file.write_text(_BUDGETED_TEAM, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(str(workspace), {**CONFIG, "budget": 123}, trace=False)
    scheduler = build_scheduler(
        ctx,
        use_worktrees=False,
        interactive=False,
        prebuild_team=True,
        team_config_path=str(team_file),
    )
    try:
        assert scheduler._role_budgets == {"lead": 3_000_000, "coder": 2_000_000}
        assert scheduler._max_budget_tokens == 5_000_000
        assert scheduler._entry_start_budget() == 3_000_000
    finally:
        run(scheduler.cleanup())


def test_client_team_refuses_a_budget_the_file_already_declares(tmp_path) -> None:
    from opencollab import OpenCollab

    team_file = tmp_path / "team.yaml"
    team_file.write_text(_BUDGETED_TEAM, encoding="utf-8")
    with pytest.raises(ValueError, match="per-role token budgets"):
        run(
            OpenCollab(tmp_path).team(
                "solve it", config=team_file, budget=1_000, trace=False, use_worktrees=False,
                prebuild_team=True,
            )
        )


def test_a_programmatic_team_still_canonicalizes_its_topology() -> None:
    """Guard for the validation this change sits beside in ``TeamConfig``.

    Edges written with a role's name in another case must resolve to the
    declared role; an edit that cut ``__post_init__`` short dropped this
    silently while every other test stayed green.
    """
    from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
    from opencollab.domain.team import Topology

    team = TeamConfig(
        roles={
            "Lead": RoleConfig(prompt="hi", tools=[]),
            "Coder": RoleConfig(prompt="hi", tools=[]),
        },
        entry="Lead",
        topology=Topology(edges={"lead": frozenset({"coder"})}),
    )
    assert team.topology.edges == {"Lead": frozenset({"Coder"})}
