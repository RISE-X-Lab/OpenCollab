"""Unit tests for the extracted Team submodules."""

from __future__ import annotations

import inspect

from opencollab.bootstrap.safety import build_workspace_safety_policy
from opencollab.core.env import LocalEnvironment
from opencollab.core.session.events import EventBus
from opencollab.team import orchestrator as orchestrator_mod
from opencollab.team.teammate_factory import TeammateConfig, build_teammate_session, split_budget
from opencollab.team import teammate_factory as teammate_factory_mod
from opencollab.tools.safety import SandboxInterceptor


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
    from opencollab.core.session import session as session_module

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
    from opencollab.core.session import session as session_module

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


def test_team_modules_do_not_import_bootstrap_safety():
    team_source = inspect.getsource(orchestrator_mod)
    teammate_source = inspect.getsource(teammate_factory_mod)

    assert "opencollab.bootstrap.safety" not in team_source
    assert "opencollab.bootstrap.safety" not in teammate_source
