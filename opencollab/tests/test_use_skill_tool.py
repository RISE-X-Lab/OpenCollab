"""Unit tests for the use_skill dispatcher tool."""

from __future__ import annotations

import asyncio

from opencollab.adapters.tools.use_skill import UseSkillTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.domain.skill import SkillManifest


def run(coro):
    return asyncio.run(coro)


class FakeSkillStore:
    """A minimal SkillStorePort for tests."""

    def __init__(self, bodies: dict[str, str]):
        self._bodies = dict(bodies)

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(
            SkillManifest(name=n, description=f"{n} desc") for n in self._bodies
        )

    def get_body(self, name: str) -> str | None:
        return self._bodies.get(name)


def _runtime():
    return ToolRuntime(environment=None, safety_policy=None, permission_policy=None)


def test_use_skill_known_name_returns_body_verbatim():
    store = FakeSkillStore({"debug": "Step 1. Step 2. The full body."})
    tool = UseSkillTool(store)
    result = run(tool.execute_with_runtime({"name": "debug"}, _runtime()))
    assert result == "Step 1. Step 2. The full body."


def test_use_skill_unknown_name_returns_available_names_without_raising():
    store = FakeSkillStore({"alpha": "a", "beta": "b"})
    tool = UseSkillTool(store)
    result = run(tool.execute_with_runtime({"name": "missing"}, _runtime()))
    assert "missing" in result
    assert "alpha" in result
    assert "beta" in result


def test_use_skill_empty_catalog_unknown_name_is_helpful():
    store = FakeSkillStore({})
    tool = UseSkillTool(store)
    result = run(tool.execute_with_runtime({"name": "x"}, _runtime()))
    assert "x" in result
    assert "No skills" in result


def test_use_skill_missing_param_does_not_raise():
    store = FakeSkillStore({"alpha": "a"})
    tool = UseSkillTool(store)
    result = run(tool.execute_with_runtime({}, _runtime()))
    # Empty name is an unknown skill, not a crash.
    assert "alpha" in result


def test_use_skill_schema_requires_name():
    tool = UseSkillTool(FakeSkillStore({}))
    assert tool.name == "use_skill"
    assert tool.parameters["required"] == ["name"]
    assert "name" in tool.parameters["properties"]
