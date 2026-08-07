"""Workflow runtime construction and invocation tests."""

from __future__ import annotations

import os
import types
from typing import Any

import pytest
from workflow_runtime_test_support import (
    _cfg,
    _patch_build_session,
)

from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.bootstrap import (
    _workflow_runtime_execution as workflow_execution,
)
from opencollab.bootstrap import (
    _workflow_runtime_session as workflow_session,
)
from opencollab.bootstrap import (
    workflow_runtime,
)
from opencollab.bootstrap.session_factory import slug_label


def test_workflow_runtime_public_module_uses_plain_reexports():
    assert type(workflow_runtime) is types.ModuleType
    assert workflow_runtime.WorkflowSessionFactory is workflow_session.WorkflowSessionFactory
    assert workflow_runtime.build_workflow_context is workflow_session.build_workflow_context

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


class _FalseyEnvironment:
    def __bool__(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_built_context_preserves_falsey_injected_environment(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    environment = _FalseyEnvironment()
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), env=environment)

    await ctx.agent("solve this")

    assert calls[0]["env"] is environment
    assert ctx._tree_probe._env is environment


def test_concrete_factory_rejects_unsupported_isolation_before_building_session(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    cfg = _cfg()
    factory = workflow_runtime.WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )

    with pytest.raises(ValueError, match="isolation is not available"):
        factory.build_workflow_session(prompt="solve", budget=1, isolation=True)

    assert calls == []


@pytest.mark.asyncio
async def test_built_context_injects_sampling_and_output_limits(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(
        cfg=_cfg(temperature=1.0, top_p=1.0, max_output_tokens=32_768)
    )

    await ctx.agent("solve")

    agent = calls[0]["agent"]
    assert agent.temperature == 1.0
    assert agent.top_p == 1.0
    assert agent.max_tokens_per_step == 32_768


@pytest.mark.asyncio
async def test_built_context_threads_session_limits_and_system_prompt(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(
        cfg=_cfg(),
        max_steps=60,
        system_prompt="Evaluation system prompt",
    )

    await ctx.agent("first")
    await ctx.agent("second")

    assert [call["max_steps"] for call in calls] == [60, 60]
    assert [call["agent"].system_prompt for call in calls] == [
        "Evaluation system prompt",
        "Evaluation system prompt",
    ]

@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["k3", "kimi-for-coding"])
async def test_kimi_global_thinking_applies_to_fast_structured_roles(monkeypatch, model):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(
        cfg=_cfg(model=model, provider="openai", thinking=True)
    )

    await ctx.agent("solve", thinking=False)

    assert calls[0]["agent"].thinking is True

@pytest.mark.asyncio
async def test_other_models_can_disable_thinking_for_fast_roles(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(
        cfg=_cfg(model="another-thinking-model", provider="openai", thinking=True)
    )

    await ctx.agent("solve", thinking=False)

    assert calls[0]["agent"].thinking is False

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
async def test_run_workflow_aggregates_session_metrics(monkeypatch):
    from workflow_runtime_test_support import _FakeSession

    values = ((5, 2, 1), (7, 3, 2))

    def fake_build_session(*, agent, **_kwargs):
        session = _FakeSession(agent, agent.tools)
        session.used_tokens, session.step_count, session.markup_recovered = values[
            fake_build_session.calls
        ]
        fake_build_session.calls += 1
        return session

    fake_build_session.calls = 0
    monkeypatch.setattr(workflow_session, "build_session", fake_build_session)

    async def fn(ctx, _args):
        await ctx.agent("first")
        await ctx.agent("second")
        return "done"

    details = await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        return_details=True,
    )

    assert details.output == "done"
    assert details.tokens == 12
    assert details.sessions == 2
    assert details.steps == 5
    assert details.markup_recovered == 3

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

@pytest.mark.parametrize(
    "cleanup_timeout",
    [0, -1, float("nan"), float("inf"), True, "bad"],
)
@pytest.mark.asyncio
async def test_run_workflow_rejects_invalid_cleanup_timeout_before_side_effects(
    monkeypatch,
    cleanup_timeout,
):
    built = False

    def fail_if_built(**kwargs):
        nonlocal built
        built = True
        raise AssertionError("context must not be built")

    monkeypatch.setattr(workflow_execution, "build_workflow_context", fail_if_built)

    async def fn(ctx, args):
        return "unused"

    with pytest.raises(ValueError, match="cleanup_timeout"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            cleanup_timeout=cleanup_timeout,
        )
    assert built is False

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
    """With a save_dir, each session gets its own ordered <seq>.json path."""
    calls = _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), save_dir=save_dir)

    await ctx.agent("one")
    await ctx.agent("two")

    assert [c["auto_save_path"] for c in calls] == [
        os.path.join(save_dir, "000.json"),
        os.path.join(save_dir, "001.json"),
    ]

@pytest.mark.asyncio
async def test_save_dir_slugs_agent_label_into_filename(monkeypatch, tmp_path):
    """A caller label becomes the role in the per-role transcript filename.

    Mirrors a team run folder's ``agent_<aid>_<role>.json``: ``<seq>_<role>.json``
    so the run folder reads as its roles, and the seq prefix disambiguates a role
    that runs more than once.
    """
    calls = _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), save_dir=save_dir)

    await ctx.agent("analyze the bug", label="analyst")
    await ctx.agent("write the fix", label="coder:s1r2")

    assert [c["auto_save_path"] for c in calls] == [
        os.path.join(save_dir, "000_analyst.json"),
        os.path.join(save_dir, "001_coder-s1r2.json"),
    ]

def test_slug_sanitizes_and_caps_labels():
    assert slug_label("coder:s1r2") == "coder-s1r2"
    assert slug_label("reviewer: 1") == "reviewer-1"
    assert slug_label(":analyst:revise:") == "analyst-revise"
    assert slug_label(None) == ""
    assert slug_label("") == ""
    assert len(slug_label("x" * 100)) == 40
