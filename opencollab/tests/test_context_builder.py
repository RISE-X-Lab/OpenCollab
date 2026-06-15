"""Unit tests for ContextBuilder — role -> Agent assembly."""

from __future__ import annotations

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.container import ContextBuilder, SpawnConfig
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.domain.context import ContextLayer, ContextPosition, LoadTiming
from opencollab.domain.team import Topology


def _cfg(model="default-model"):
    return SpawnConfig(
        model=model,
        provider="openai",
        api_key="k",
        base_url=None,
        llm_timeout=600.0,
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


# --- build_plan: layered context sources -----------------------------------


def _sources_by_name(plan):
    return {s.name: s for s in plan.sources}


def test_build_plan_identity_is_startup_system_source_with_role_prompt():
    plan = ContextBuilder(_team(), _cfg()).build_plan("coder")
    identity = _sources_by_name(plan)["identity"]
    assert identity.layer is ContextLayer.IDENTITY
    assert identity.timing is LoadTiming.STARTUP
    assert identity.position is ContextPosition.SYSTEM
    assert identity.content == "Coder."


def test_build_plan_team_section_is_startup_system_source_when_present():
    plan = ContextBuilder(_team(), _cfg()).build_plan("lead")
    team = _sources_by_name(plan)["team"]
    assert team.position is ContextPosition.SYSTEM
    assert "## Your team" in team.content


def test_build_plan_task_is_startup_user_context_source_when_given():
    plan = ContextBuilder(_team(), _cfg()).build_plan("coder", task="build it", context="ctx")
    task = _sources_by_name(plan)["task"]
    assert task.layer is ContextLayer.TASK
    assert task.timing is LoadTiming.STARTUP
    assert task.position is ContextPosition.USER_CONTEXT
    assert "build it" in task.content and "ctx" in task.content
    assert plan.startup_user_messages() == [
        {
            "role": "user",
            "content": task.content,
            "_ctx": {"layer": "task", "priority": task.effective_priority},
        }
    ]


def test_build_plan_omits_task_source_when_no_task():
    plan = ContextBuilder(_team(), _cfg()).build_plan("coder")
    assert "task" not in _sources_by_name(plan)
    assert plan.startup_user_messages() == []


def test_build_plan_registers_reserved_layers_as_deferred_not_loaded():
    plan = ContextBuilder(_team(), _cfg()).build_plan("coder", task="t")
    deferred = {s.name: s for s in plan.deferred_sources()}
    assert set(deferred) == {"project", "memory", "tool_meta"}
    # reserved layers carry a loader_key and contribute no content this period
    for s in deferred.values():
        assert s.loader_key is not None
        assert s.content == ""
    # ...and none of them leak into the assembled messages
    bodies = [m["content"] for m in plan.messages()]
    assert all("loader" not in b for b in bodies)


def test_build_agent_system_prompt_matches_plan_system_sources():
    builder = ContextBuilder(_team(), _cfg())
    plan = builder.build_plan("lead")
    agent = builder.build_agent("lead", scheduler=SCHED, plan=plan)
    assert agent.system_prompt == plan.system_prompt()
