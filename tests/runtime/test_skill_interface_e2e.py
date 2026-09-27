"""Headline end-to-end test for the P1 skill interface.

Wires a real ``FileSkillStore`` (from a tmp skills/ dir) through ``ContextBuilder``
for a role granted ``use_skill``, and asserts:
  1. the catalog text appears in the assembled system prompt, and
  2. invoking the bound ``UseSkillTool`` with a catalog name returns that
     skill's body.

No shipping config is touched — the role/team are constructed in-test.
"""

from __future__ import annotations

import asyncio

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.skills.file_skill_store import FileSkillStore
from opencollab.adapters.tools.use_skill import UseSkillTool
from opencollab.application.event_bus import EventBus
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.container import build_skill_store
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.session_factory import DefaultSessionFactory
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.domain.team import Topology


def run(coro):
    return asyncio.run(coro)


def _write_skill(root, name, *, description, body):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def _cfg():
    return SpawnConfig(
        model="default-model",
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
            "specialist": RoleConfig(
                prompt="Specialist base prompt.",
                model=None,
                tools=["bash", "use_skill"],
            ),
        },
        topology=Topology(),
    )


def test_skill_catalog_in_system_prompt_and_body_retrievable(tmp_path):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "debug-flaky-tests",
        description="Systematically root-cause flaky tests.",
        body="1. Run the test 100 times. 2. Bisect. 3. Stabilize.",
    )

    # Wire exactly as the composition root does: build_skill_store resolves the
    # workspace skills/ dir, then ContextBuilder receives it.
    store = build_skill_store(str(tmp_path))
    assert isinstance(store, FileSkillStore)

    builder = ContextBuilder(_team(), _cfg(), skill_store=store)

    # 1. Catalog appears in the assembled system prompt.
    agent = builder.build_agent("specialist")
    assert "Specialist base prompt." in agent.system_prompt
    assert "debug-flaky-tests" in agent.system_prompt
    assert "Systematically root-cause flaky tests." in agent.system_prompt
    assert "use_skill" in agent.system_prompt

    # 2. The bound dispatcher returns the skill's full body.
    use_skill = next(t for t in agent.tools if t.name == "use_skill")
    assert isinstance(use_skill, UseSkillTool)
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)
    body = run(use_skill.execute_with_runtime({"name": "debug-flaky-tests"}, runtime))
    assert body == "1. Run the test 100 times. 2. Bisect. 3. Stabilize."


def test_no_skills_dir_is_unchanged_behavior(tmp_path):
    # A workspace without a skills/ dir → NullSkillStore → no catalog, and the
    # role still resolves its other tools (use_skill simply offers nothing).
    store = build_skill_store(str(tmp_path))
    builder = ContextBuilder(_team(), _cfg(), skill_store=store)
    agent = builder.build_agent("specialist")
    assert "Specialist base prompt." in agent.system_prompt
    # No catalog header injected.
    assert "## Skills" not in agent.system_prompt


def test_session_factory_refreshes_skill_catalog_for_each_new_session(tmp_path):
    factory = DefaultSessionFactory(
        _cfg(),
        team_cfg=_team(),
        lead_workspace=str(tmp_path),
    )
    first = factory.build_spawn_session(
        role="specialist",
        env=LocalEnvironment(str(tmp_path)),
        budget=10_000,
        aid=1,
    )

    _write_skill(
        tmp_path / "skills",
        "new-skill",
        description="Added during the team run.",
        body="Use the newly added procedure.",
    )
    second = factory.build_spawn_session(
        role="specialist",
        env=LocalEnvironment(str(tmp_path)),
        budget=10_000,
        aid=2,
    )

    assert "new-skill" not in first.agent.system_prompt
    assert "new-skill" in second.agent.system_prompt
