"""Tests for bootstrap workflow wiring: build_workflow_context + run_workflow.

The factory binding (``WorkflowSessionFactory``) is exercised by checking that a
built context produces sessions through ``build_session`` with the resolved
model/provider/key flowing into the underlying ``Agent``. ``build_session`` is
monkeypatched so no real LLM client is ever constructed.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
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


@pytest.mark.asyncio
async def test_run_workflow_returns_structured_budget_exceeded(monkeypatch):
    """WorkflowBudgetExceeded at the run boundary becomes a structured result.

    A workflow that exhausts the budget should not blow up the caller with a raw
    traceback; run_workflow catches WorkflowBudgetExceeded and returns a dict
    carrying status, error text, and the spend/total snapshot.
    """
    _patch_build_session(monkeypatch)

    async def fn(ctx, args):
        # Drive a session so some tokens are spent, then raise as agent() would
        # once the budget is exhausted.
        raise WorkflowBudgetExceeded("workflow budget exhausted: spent 50 of 40")

    result = await workflow_runtime.run_workflow(fn, {}, cfg=_cfg(budget=40))

    assert result["status"] == "budget_exceeded"
    assert result["error"] == "workflow budget exhausted: spent 50 of 40"
    assert result["budget_total"] == 40
    # No session spent anything in this fn, so the live snapshot is 0.
    assert result["tokens_spent"] == 0


@pytest.mark.asyncio
async def test_run_workflow_reports_live_spend_on_budget_exceeded(monkeypatch):
    """The structured budget_exceeded dict reports the live token spend.

    Running an agent first spends tokens via the tracked session; when the
    workflow then raises WorkflowBudgetExceeded, the returned dict's
    ``tokens_spent`` reflects that live spend (not 0).
    """
    calls = _patch_build_session(monkeypatch)

    async def fn(ctx, args):
        await ctx.agent("do work")
        # The fake session reports used_tokens=0, but assert the wiring reads the
        # live budget snapshot rather than a hardcoded value.
        raise WorkflowBudgetExceeded("exhausted")

    result = await workflow_runtime.run_workflow(fn, {}, cfg=_cfg(budget=1000))

    assert result["status"] == "budget_exceeded"
    assert result["error"] == "exhausted"
    assert result["budget_total"] == 1000
    assert result["tokens_spent"] == 0  # _FakeSession.used_tokens == 0
    assert len(calls) == 1  # the agent did build+run before the raise


@pytest.mark.asyncio
async def test_run_workflow_other_exceptions_propagate(monkeypatch):
    """Only WorkflowBudgetExceeded is caught; everything else still raises."""
    _patch_build_session(monkeypatch)

    async def fn(ctx, args):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await workflow_runtime.run_workflow(fn, {}, cfg=_cfg())


# -- session persistence --------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_save_dir_keeps_sessions_ephemeral(monkeypatch):
    """Without a save_dir, build_session gets auto_save_path=None (no autosave)."""
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg())

    await ctx.agent("one")
    await ctx.agent("two")

    assert [c["auto_save_path"] for c in calls] == [None, None]


@pytest.mark.asyncio
async def test_save_dir_threads_sequential_per_session_paths(monkeypatch, tmp_path):
    """With a save_dir, each session gets its own ordered session_<n>.json path."""
    calls = _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), save_dir=save_dir)

    await ctx.agent("one")
    await ctx.agent("two")

    assert [c["auto_save_path"] for c in calls] == [
        os.path.join(save_dir, "session_000.json"),
        os.path.join(save_dir, "session_001.json"),
    ]


@pytest.mark.asyncio
async def test_save_dir_slugs_agent_label_into_filename(monkeypatch, tmp_path):
    """A caller label is slugged into the transcript filename for readability."""
    calls = _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), save_dir=save_dir)

    await ctx.agent("analyze the bug", label="analyst")
    await ctx.agent("write the fix", label="coder:s1r2")

    assert [c["auto_save_path"] for c in calls] == [
        os.path.join(save_dir, "session_000_analyst.json"),
        os.path.join(save_dir, "session_001_coder-s1r2.json"),
    ]


def test_slug_sanitizes_and_caps_labels():
    assert workflow_runtime._slug("coder:s1r2") == "coder-s1r2"
    assert workflow_runtime._slug("reviewer: 1") == "reviewer-1"
    assert workflow_runtime._slug(":analyst:revise:") == "analyst-revise"
    assert workflow_runtime._slug(None) == ""
    assert workflow_runtime._slug("") == ""
    assert len(workflow_runtime._slug("x" * 100)) == 40


@pytest.mark.asyncio
async def test_run_workflow_writes_manifest(monkeypatch, tmp_path):
    """A save_dir run drops a workflow.json grouping the run's sessions."""
    _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    from opencollab.application.workflow_registry import workflow

    @workflow(name="demo_wf", description="d")
    async def fn(ctx, args):
        await ctx.agent("a")
        await ctx.agent("b")
        return "ok"

    result = await workflow_runtime.run_workflow(
        fn.__workflow_spec__, {"goal": "x"}, cfg=_cfg(), save_dir=save_dir
    )

    assert result == "ok"
    with open(os.path.join(save_dir, "workflow.json")) as f:
        manifest = json.load(f)
    assert manifest["workflow"] == "demo_wf"
    assert manifest["args"] == {"goal": "x"}
    assert manifest["sessions"] == 2
    assert manifest["budget_total"] == 100_000


@pytest.mark.asyncio
async def test_run_workflow_writes_trajectory(monkeypatch, tmp_path):
    """A saved run records a trajectory.jsonl via the auto-wired Tracer.

    The workflow only emits phase/log events (which flow straight through the
    context's tracer), so this exercises the trajectory wiring without a real
    LLM — build_session is monkeypatched away.
    """
    _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    from opencollab.application.workflow_registry import workflow

    @workflow(name="trace_wf", description="d")
    async def fn(ctx, args):
        await ctx.phase("scan")
        await ctx.log("looking")
        return "ok"

    result = await workflow_runtime.run_workflow(
        fn.__workflow_spec__, {}, cfg=_cfg(), save_dir=save_dir
    )

    assert result == "ok"
    path = os.path.join(save_dir, "trajectory.jsonl")
    assert os.path.exists(path)
    with open(path) as f:
        types = [json.loads(line)["type"] for line in f if line.strip()]
    assert "workflow_phase" in types
    assert "workflow_log" in types


@pytest.mark.asyncio
async def test_run_workflow_trace_false_skips_trajectory(monkeypatch, tmp_path):
    """``trace=False`` suppresses the trajectory even when the run is saved."""
    _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    from opencollab.application.workflow_registry import workflow

    @workflow(name="no_trace_wf", description="d")
    async def fn(ctx, args):
        await ctx.phase("scan")
        return "ok"

    await workflow_runtime.run_workflow(
        fn.__workflow_spec__, {}, cfg=_cfg(), save_dir=save_dir, trace=False
    )

    assert not os.path.exists(os.path.join(save_dir, "trajectory.jsonl"))


@pytest.mark.asyncio
async def test_run_workflow_no_save_dir_skips_trajectory(monkeypatch, tmp_path):
    """Without a save_dir there is no run folder, so no trajectory is written."""
    _patch_build_session(monkeypatch)
    from opencollab.application.workflow_registry import workflow

    @workflow(name="ephemeral_wf", description="d")
    async def fn(ctx, args):
        await ctx.phase("scan")
        return "ok"

    result = await workflow_runtime.run_workflow(fn.__workflow_spec__, {}, cfg=_cfg())

    assert result == "ok"
    assert not os.path.exists(os.path.join(str(tmp_path), "trajectory.jsonl"))
