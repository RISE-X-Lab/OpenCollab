"""Unit tests for the extracted Scheduler submodules."""

from __future__ import annotations

import asyncio
import inspect

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools import spawn as spawn_mod
from opencollab.adapters.tools.spawn import SpawnAgentTool, SpawnWithReviewTool
from opencollab.application import scheduler as scheduler_mod
from opencollab.application.event_bus import EventBus
from opencollab.application.tool_execution import DeferredCall, ToolRuntime
from opencollab.bootstrap.container import SpawnConfig, build_spawn_session, build_workspace_safety_policy
from opencollab.domain.scheduler import split_budget


def run(coro):
    return asyncio.run(coro)


class FakeScheduler:
    def __init__(self):
        self.spawn_calls = []
        self.review_calls = []

    async def spawn(self, parent_aid, role, task, context="", tool_call_id=None):
        self.spawn_calls.append((parent_aid, role, task, context, tool_call_id))
        return 42  # return a fake aid

    def inflight_spawn(self, role, task):
        return None  # never deduped in this fake

    async def spawn_with_review(self, parent_aid, task, context="", max_iterations=3):
        self.review_calls.append((parent_aid, task, context, max_iterations))
        return f"reviewed {task} [{context}] x{max_iterations}"


# split_budget arithmetic — used to be inline in Team.delegate, hard to test.

def test_split_budget_fresh_team_reserves_quarter_for_lead():
    # Total 400_000, nothing used → lead reserve = max(10_000, 100_000) = 100_000
    # spawn = max(10_000, 400_000 - 100_000) = 300_000
    assert split_budget(total=400_000, used=0) == 300_000


def test_split_budget_with_prior_usage_subtracts():
    # Total 400_000, used 150_000 → remaining 250_000
    # reserve = min(100_000, 240_000) = 100_000
    # spawn = max(10_000, 250_000 - 100_000) = 150_000
    assert split_budget(total=400_000, used=150_000) == 150_000


def test_split_budget_floors_spawn_at_10k():
    # Total 400_000, used 395_000 → remaining max(10_000, 5_000) = 10_000
    # reserve = min(100_000, 0) = 0
    # spawn = max(10_000, 10_000 - 0) = 10_000
    assert split_budget(total=400_000, used=395_000) == 10_000


def test_split_budget_small_total_still_floors_at_10k():
    # Total 30_000, used 0 → remaining 30_000
    # reserve = min(max(10_000, 7_500), max(0, 20_000)) = 10_000
    # spawn = max(10_000, 20_000) = 20_000
    assert split_budget(total=30_000, used=0) == 20_000


def test_build_spawn_session_wires_environment_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module

    class FakeLLMClient:
        pass

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())

    env = LocalEnvironment(str(tmp_path))
    cfg = SpawnConfig(
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
        safety_policy_factory=build_workspace_safety_policy,
    )

    session = build_spawn_session(role="coder", env=env, cfg=cfg, budget=50_000)

    policy = session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())


def test_build_spawn_session_without_factory_does_not_build_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module

    class FakeLLMClient:
        pass

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())

    env = LocalEnvironment(str(tmp_path))
    cfg = SpawnConfig(
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
    )

    session = build_spawn_session(role="coder", env=env, cfg=cfg, budget=50_000)

    assert session.tool_execution.safety_policy is None


def test_build_spawn_session_seeds_task_as_first_user_message(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: object())

    env = LocalEnvironment(str(tmp_path))
    cfg = SpawnConfig(
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
    )

    session = build_spawn_session(
        role="coder", env=env, cfg=cfg, budget=50_000, task="build it", context="ctx"
    )

    assert session.messages[0]["role"] == "system"
    assert session.messages[1]["role"] == "user"
    assert "build it" in session.messages[1]["content"]
    assert "ctx" in session.messages[1]["content"]
    # No task -> no extra user message (lead-style per-turn seeding path).
    bare = build_spawn_session(role="coder", env=env, cfg=cfg, budget=50_000)
    assert [m["role"] for m in bare.messages] == ["system"]


def test_spawn_agent_tool_uses_runtime_native_execution():
    scheduler = FakeScheduler()
    tool = SpawnAgentTool(scheduler)
    runtime = ToolRuntime(
        environment=None, safety_policy=None, permission_policy=None, aid=0, tool_call_id="call-7"
    )

    result = run(
        tool.execute_with_runtime(
            {"role": "coder", "task": "implement", "context": "ctx"},
            runtime,
        )
    )

    # Defers with a DeferredCall carrying the child aid (not a string) so the
    # deferral path can register a pending row; the tool_call_id is threaded
    # through for completion routing.
    assert result == DeferredCall(ref=42)
    assert scheduler.spawn_calls == [(0, "coder", "implement", "ctx", "call-7")]
    assert "execute" not in SpawnAgentTool.__dict__


def test_spawn_with_review_tool_uses_runtime_native_execution():
    scheduler = FakeScheduler()
    tool = SpawnWithReviewTool(scheduler)
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None, aid=0)

    result = run(
        tool.execute_with_runtime(
            {"task": "change code", "context": "ctx", "max_iterations": 2},
            runtime,
        )
    )

    assert result == "reviewed change code [ctx] x2"
    assert scheduler.review_calls == [(0, "change code", "ctx", 2)]
    assert "execute" not in SpawnWithReviewTool.__dict__


def test_scheduler_modules_do_not_import_bootstrap_safety():
    scheduler_source = inspect.getsource(scheduler_mod)
    spawn_source = inspect.getsource(spawn_mod)

    assert "opencollab.bootstrap.safety" not in scheduler_source
    assert "opencollab.bootstrap.safety" not in spawn_source
