"""Unit tests for ContextBuilder — role -> Agent assembly."""

from __future__ import annotations

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.container import ContextBuilder, SpawnConfig
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.domain.team import Topology


def _cfg(model="default-model"):
    return SpawnConfig(
        model=model,
        provider="openai",
        api_key="k",
        base_url=None,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
    )


def _team():
    return TeamConfig(
        roles={
            "lead": RoleConfig(
                prompt="Lead base prompt.",
                model=None,
                tools=["bash", "spawn_agent", "message_agent", "team_status", "ask_user"],
            ),
            "coder": RoleConfig(prompt="Coder.", model="coder-model", tools=["bash", "file_read"]),
            "reviewer": RoleConfig(prompt="Reviewer.", model=None, tools=["file_read", "grep"]),
        },
        topology=Topology(edges={"lead": frozenset({"coder", "reviewer"}), "coder": frozenset({"reviewer"})}),
    )


SCHED = object()  # tools only store the scheduler; build_agent never calls it.


def test_lead_prompt_gets_topology_aware_team_section():
    agent = ContextBuilder(_team(), _cfg()).build_agent("lead", scheduler=SCHED, interactive=True)
    assert "Lead base prompt." in agent.system_prompt
    assert "## Your team" in agent.system_prompt
    assert "- coder" in agent.system_prompt
    assert "- reviewer" in agent.system_prompt
    assert "team_status" in agent.system_prompt


def test_role_without_coordination_tools_gets_no_team_section():
    # reviewer has explicit topology targets but no coordination tools.
    agent = ContextBuilder(_team(), _cfg()).build_agent("reviewer", scheduler=SCHED)
    assert "## Your team" not in agent.system_prompt


def test_ask_user_dropped_when_non_interactive():
    builder = ContextBuilder(_team(), _cfg())
    headless = builder.build_agent("lead", scheduler=SCHED, interactive=False)
    interactive = builder.build_agent("lead", scheduler=SCHED, interactive=True)
    assert "ask_user" not in {t.name for t in headless.tools}
    assert "ask_user" in {t.name for t in interactive.tools}


def test_model_override_and_default():
    builder = ContextBuilder(_team(), _cfg(model="default-model"))
    assert builder.build_agent("coder", scheduler=SCHED).model == "coder-model"
    assert builder.build_agent("reviewer", scheduler=SCHED).model == "default-model"


def test_unknown_role_falls_back_to_generic_spec():
    agent = ContextBuilder(_team(), _cfg()).build_agent("data-scientist", scheduler=SCHED)
    assert {t.name for t in agent.tools} == {"bash", "file_read", "file_write", "grep"}


def test_unknown_tool_name_raises():
    team = TeamConfig(
        roles={"x": RoleConfig(prompt="x", model=None, tools=["bash", "frobnicate"])},
        topology=Topology(),
    )
    with pytest.raises(ValueError, match="Unknown tool 'frobnicate'"):
        ContextBuilder(team, _cfg()).build_agent("x")


def test_scheduler_bound_tool_requires_scheduler():
    team = TeamConfig(
        roles={"x": RoleConfig(prompt="x", model=None, tools=["spawn_agent"])},
        topology=Topology(),
    )
    with pytest.raises(ValueError, match="requires a scheduler"):
        ContextBuilder(team, _cfg()).build_agent("x", scheduler=None)
