"""Unit tests for the extracted Team submodules."""

from __future__ import annotations

import asyncio
import inspect

from opencollab.application.tool_runtime import ToolRuntime
from opencollab.bootstrap.safety import build_workspace_safety_policy
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.event_bus import EventBus
from opencollab.domain.team import split_budget
from opencollab.application import team as orchestrator_mod
from opencollab.adapters.tools import delegation as delegation_mod
from opencollab.adapters.tools.delegation import DelegateTaskTool, DelegateWithReviewTool
from opencollab.bootstrap.teammate_factory import TeammateConfig, build_teammate_session
from opencollab.bootstrap import teammate_factory as teammate_factory_mod
from opencollab.adapters.safety import SandboxInterceptor


def run(coro):
    return asyncio.run(coro)


class FakeTeam:
    def __init__(self):
        self.delegate_calls = []
        self.review_calls = []

    async def delegate(self, role, task, context=""):
        self.delegate_calls.append((role, task, context))
        return f"delegated {role}: {task} [{context}]"

    async def delegate_with_review(self, task, context="", max_iterations=3):
        self.review_calls.append((task, context, max_iterations))
        return f"reviewed {task} [{context}] x{max_iterations}"


# split_budget arithmetic — used to be inline in Team.delegate, hard to test.

def test_split_budget_fresh_team_reserves_quarter_for_lead():
    # Total 400_000, nothing used → lead reserve = max(10_000, 100_000) = 100_000
    # teammate = max(10_000, 400_000 - 100_000) = 300_000
    assert split_budget(total=400_000, used=0) == 300_000


def test_split_budget_with_prior_usage_subtracts():
    # Total 400_000, used 150_000 → remaining 250_000
    # reserve = min(100_000, 240_000) = 100_000
    # teammate = max(10_000, 250_000 - 100_000) = 150_000
    assert split_budget(total=400_000, used=150_000) == 150_000


def test_split_budget_floors_teammate_at_10k():
    # Total 400_000, used 395_000 → remaining max(10_000, 5_000) = 10_000
    # reserve = min(100_000, 0) = 0
    # teammate = max(10_000, 10_000 - 0) = 10_000
    assert split_budget(total=400_000, used=395_000) == 10_000


def test_split_budget_small_total_still_floors_at_10k():
    # Total 30_000, used 0 → remaining 30_000
    # reserve = min(max(10_000, 7_500), max(0, 20_000)) = 10_000
    # teammate = max(10_000, 20_000) = 20_000
    assert split_budget(total=30_000, used=0) == 20_000


def test_build_teammate_session_wires_environment_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module

    class FakeLLMClient:
        pass

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())

    env = LocalEnvironment(str(tmp_path))
    cfg = TeammateConfig(
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        base_url=None,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
        repo_map=None,
        safety_policy_factory=build_workspace_safety_policy,
    )

    session = build_teammate_session(role="coder", env=env, cfg=cfg, budget=50_000)

    policy = session.tool_processor.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())


def test_build_teammate_session_without_factory_does_not_build_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module

    class FakeLLMClient:
        pass

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())

    env = LocalEnvironment(str(tmp_path))
    cfg = TeammateConfig(
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        base_url=None,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
        repo_map=None,
    )

    session = build_teammate_session(role="coder", env=env, cfg=cfg, budget=50_000)

    assert session.tool_processor.safety_policy is None


def test_delegate_task_tool_uses_runtime_native_execution():
    team = FakeTeam()
    tool = DelegateTaskTool(team)
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        tool.execute_with_runtime(
            {"role": "coder", "task": "implement", "context": "ctx"},
            runtime,
        )
    )

    assert result == "delegated coder: implement [ctx]"
    assert team.delegate_calls == [("coder", "implement", "ctx")]
    assert "execute" not in DelegateTaskTool.__dict__


def test_delegate_with_review_tool_uses_runtime_native_execution():
    team = FakeTeam()
    tool = DelegateWithReviewTool(team)
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        tool.execute_with_runtime(
            {"task": "change code", "context": "ctx", "max_iterations": 2},
            runtime,
        )
    )

    assert result == "reviewed change code [ctx] x2"
    assert team.review_calls == [("change code", "ctx", 2)]
    assert "execute" not in DelegateWithReviewTool.__dict__


def test_team_modules_do_not_import_bootstrap_safety():
    team_source = inspect.getsource(orchestrator_mod)
    teammate_source = inspect.getsource(teammate_factory_mod)
    delegation_source = inspect.getsource(delegation_mod)

    assert "opencollab.bootstrap.safety" not in team_source
    assert "opencollab.bootstrap.safety" not in teammate_source
    assert "opencollab.bootstrap.safety" not in delegation_source
