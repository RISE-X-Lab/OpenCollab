"""Tests for bootstrap workflow wiring: build_workflow_context + run_workflow.

The factory binding (``WorkflowSessionFactory``) is exercised by checking that a
built context produces sessions through ``build_session`` with the resolved
model/provider/key flowing into the underlying ``Agent``. ``build_session`` is
monkeypatched so no real LLM client is ever constructed.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap import workflow_runtime


class _FakeSession:
    def __init__(self, agent: Any, tools: Any) -> None:
        self.agent = agent
        self.tools = tools
        self.used_tokens = 0
        self.prompt: str | None = None

    async def add_user_message(self, content: str) -> None:
        self.prompt = content

    async def run_loop(self) -> str:
        return "fake-reply"


def _patch_build_session(monkeypatch) -> list[dict[str, Any]]:
    """Capture every build_session call's kwargs; return fake sessions."""
    calls: list[dict[str, Any]] = []

    def fake_build_session(*, agent, **kwargs):
        calls.append({"agent": agent, **kwargs})
        return _FakeSession(agent, agent.tools)

    monkeypatch.setattr(workflow_runtime, "build_session", fake_build_session)
    return calls


def _cfg(**overrides) -> dict[str, Any]:
    base = {
        "model": "test-model",
        "provider": "anthropic",
        "api_key": "resolved-key",
        "base_url": "https://example.test",
        "budget": 100_000,
    }
    base.update(overrides)
    return base


def test_build_workflow_context_returns_context(monkeypatch):
    _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg())
    assert isinstance(ctx, WorkflowContext)


@pytest.mark.asyncio
async def test_built_context_agent_runs_session_with_resolved_llm(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), max_concurrency=2)

    result = await ctx.agent("solve this")

    assert result == "fake-reply"
    assert len(calls) == 1
    agent = calls[0]["agent"]
    assert agent.model == "test-model"
    assert agent.provider == "anthropic"
    assert agent.api_key == "resolved-key"
    assert agent.base_url == "https://example.test"
    # The prompt is seeded as the agent's first user message.
    # The per-session budget is the remaining workflow budget.
    assert calls[0]["max_budget_tokens"] == 100_000


@pytest.mark.asyncio
async def test_built_context_threads_caller_tools(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg())

    sentinel_tool = object()
    await ctx.agent("go", tools=[sentinel_tool])

    # The caller's tools become the one-shot agent's toolset.
    assert sentinel_tool in calls[0]["agent"].tools


@pytest.mark.asyncio
async def test_run_workflow_invokes_fn_with_context_and_args(monkeypatch):
    _patch_build_session(monkeypatch)
    seen: dict[str, Any] = {}

    async def fn(ctx, args):
        seen["ctx"] = ctx
        seen["args"] = args
        return {"echo": args["x"]}

    result = await workflow_runtime.run_workflow(fn, {"x": 42}, cfg=_cfg())

    assert result == {"echo": 42}
    assert isinstance(seen["ctx"], WorkflowContext)
    assert seen["args"] == {"x": 42}


@pytest.mark.asyncio
async def test_run_workflow_accepts_a_workflow_spec(monkeypatch):
    _patch_build_session(monkeypatch)
    from opencollab.application.workflow_registry import workflow

    @workflow(name="spec_wf", description="d")
    async def fn(ctx, args):
        return "spec-ran"

    result = await workflow_runtime.run_workflow(
        fn.__workflow_spec__, {}, cfg=_cfg()
    )
    assert result == "spec-ran"
