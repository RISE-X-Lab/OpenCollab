"""Unit tests for ContextBuilder — role -> Agent assembly."""

from __future__ import annotations

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.container import ContextBuilder, SpawnConfig
from opencollab.bootstrap.team_config import BASE_TOOL_NAMES, RoleConfig, TeamConfig
from opencollab.domain.context import ContextLayer, ContextPosition, LoadTiming
from opencollab.domain.skill import SkillManifest
from opencollab.domain.team import Topology


class FakeSkillStore:
    """A SkillStorePort holding a fixed set of skills (or none)."""

    def __init__(self, skills: dict[str, str] | None = None):
        self._skills = dict(skills or {})

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(
            SkillManifest(name=n, description=d) for n, d in self._skills.items()
        )

    def get_body(self, name: str) -> str | None:
        # body keyed off name for the headline test
        return f"BODY OF {name}" if name in self._skills else None


def _cfg(model="default-model", temperature=0.2):
    return SpawnConfig(
        model=model,
        provider="openai",
        api_key="k",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
        temperature=temperature,
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


def test_temperature_global_default_applies_when_role_unset():
    # No role in _team() sets a temperature → all inherit the SpawnConfig value.
    builder = ContextBuilder(_team(), _cfg(temperature=0.3))
    assert builder.build_agent("coder", scheduler=SCHED).temperature == 0.3
    assert builder.build_agent("reviewer", scheduler=SCHED).temperature == 0.3


def test_temperature_role_override_and_zero_is_honored():
    team = TeamConfig(
        roles={
            "hot": RoleConfig(prompt="h", model=None, temperature=0.9, tools=["bash"]),
            "cold": RoleConfig(prompt="c", model=None, temperature=0.0, tools=["bash"]),
            "plain": RoleConfig(prompt="p", model=None, tools=["bash"]),
        },
        topology=Topology(),
    )
    builder = ContextBuilder(team, _cfg(temperature=0.3))
    assert builder.build_agent("hot").temperature == 0.9
    # 0.0 is a meaningful override (fully deterministic) — it must NOT be treated
    # as "unset" and fall back to the global 0.3.
    assert builder.build_agent("cold").temperature == 0.0
    assert builder.build_agent("plain").temperature == 0.3


def test_unknown_role_falls_back_to_generic_spec():
    agent = ContextBuilder(_team(), _cfg()).build_agent("data-scientist", scheduler=SCHED)
    # Ad-hoc roles get the registry-derived worker bundle; this non-interactive
    # build drops ``ask_user`` (no coordination/skill tools to begin with).
    assert {t.name for t in agent.tools} == set(BASE_TOOL_NAMES) - {"ask_user"}


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


# --- build_plan: skill catalog injection ------------------------------------


def _team_with_skill_role(tools):
    return TeamConfig(
        roles={"specialist": RoleConfig(prompt="Specialist.", model=None, tools=tools)},
        topology=Topology(),
    )


def test_skill_catalog_emitted_when_role_has_use_skill_and_store_nonempty():
    store = FakeSkillStore({"alpha": "Alpha desc.", "beta": "Beta desc."})
    team = _team_with_skill_role(["bash", "use_skill"])
    plan = ContextBuilder(team, _cfg(), skill_store=store).build_plan("specialist")
    skills = _sources_by_name(plan)["skills"]
    assert skills.layer is ContextLayer.SKILL
    assert skills.position is ContextPosition.SYSTEM
    assert skills.timing is LoadTiming.STARTUP
    # The catalog lists each skill name + description and tells the model to invoke.
    assert "use_skill" in skills.content
    assert "- alpha: Alpha desc." in skills.content
    assert "- beta: Beta desc." in skills.content
    # SYSTEM source folds into the assembled system prompt.
    assert skills.content in plan.system_prompt()


def test_skill_catalog_not_emitted_when_role_lacks_use_skill():
    store = FakeSkillStore({"alpha": "Alpha desc."})
    team = _team_with_skill_role(["bash"])  # no use_skill
    plan = ContextBuilder(team, _cfg(), skill_store=store).build_plan("specialist")
    assert "skills" not in _sources_by_name(plan)
    assert "alpha" not in plan.system_prompt()


def test_skill_catalog_not_emitted_when_store_empty():
    store = FakeSkillStore({})  # use_skill granted, but no skills exist
    team = _team_with_skill_role(["bash", "use_skill"])
    plan = ContextBuilder(team, _cfg(), skill_store=store).build_plan("specialist")
    assert "skills" not in _sources_by_name(plan)


def test_no_skill_store_defaults_to_empty_no_catalog():
    # Default ContextBuilder (no skill_store) → NullSkillStore → no catalog even
    # if the role names use_skill.
    team = _team_with_skill_role(["bash", "use_skill"])
    plan = ContextBuilder(team, _cfg()).build_plan("specialist")
    assert "skills" not in _sources_by_name(plan)


def test_build_agent_binds_use_skill_tool_when_store_provided():
    store = FakeSkillStore({"alpha": "Alpha desc."})
    team = _team_with_skill_role(["bash", "use_skill"])
    agent = ContextBuilder(team, _cfg(), skill_store=store).build_agent("specialist")
    assert "use_skill" in {t.name for t in agent.tools}
