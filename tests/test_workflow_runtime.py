"""Workflow runtime construction and invocation tests."""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path
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


def _git_workspace(path: Path) -> Path:
    """A one-commit git repository: worktrees need something to branch from."""
    path.mkdir(parents=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.com"),
        ("config", "user.name", "OpenCollab Tests"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "base"], cwd=path, check=True, capture_output=True
    )
    return path


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


def test_a_supplied_workspace_is_the_one_the_session_is_built_on(monkeypatch):
    """``env`` wins over the run-wide environment, which is what isolation is.

    Without this the flag would travel while the agent kept editing the run's
    shared tree, and a run could report an isolated agent that was not one.
    """
    calls = _patch_build_session(monkeypatch)
    cfg = _cfg()
    factory = workflow_runtime.WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )
    own_workspace = object()

    factory.build_workflow_session(
        prompt="solve", budget=1, isolation=True, env=own_workspace
    )

    assert calls[0]["env"] is own_workspace


def test_a_run_that_isolates_nobody_never_opens_a_worktree(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    cfg = _cfg()
    factory = workflow_runtime.WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )

    factory.build_workflow_session(prompt="solve", budget=1)

    assert calls[0]["env"] is not None
    assert factory._worktree_pool is None


@pytest.mark.asyncio
async def test_an_isolated_workflow_agent_edits_where_no_sibling_can_see(tmp_path):
    """The whole point, stated as behaviour rather than as a type check.

    Two isolated agents get two working trees, and a file one writes is absent
    from the other's and from the workspace they both branched from. That
    absence is what makes a handoff between them have to be carried by something
    the run records — a commit sha — instead of by the file system.
    """
    workspace = _git_workspace(tmp_path / "repo")
    factory = workflow_runtime.WorkflowSessionFactory(
        model="test-model",
        provider="anthropic",
        api_key="k",  # pragma: allowlist secret
        base_url="https://example.test",
        workspace=str(workspace),
    )

    coder = await factory.acquire_isolated_env(label="coder")
    tester = await factory.acquire_isolated_env(label="tester")
    try:
        assert coder.workspace != tester.workspace != str(workspace)
        await coder.write_file("only_the_coder_wrote_this.txt", "patch")

        assert not (Path(tester.workspace) / "only_the_coder_wrote_this.txt").exists()
        assert not (workspace / "only_the_coder_wrote_this.txt").exists()
    finally:
        await factory.release_isolated_envs()

    assert not Path(coder.workspace).exists()


@pytest.mark.asyncio
async def test_releasing_isolated_workspaces_twice_is_not_an_error(tmp_path):
    factory = workflow_runtime.WorkflowSessionFactory(
        model="test-model",
        provider="anthropic",
        api_key="k",  # pragma: allowlist secret
        base_url="https://example.test",
        workspace=str(_git_workspace(tmp_path / "repo")),
    )
    await factory.acquire_isolated_env(label="coder")

    await factory.release_isolated_envs()
    await factory.release_isolated_envs()


def test_explicit_thinking_false_disables_responses_reasoning_effort(monkeypatch):
    _patch_build_session(monkeypatch)
    factory = workflow_runtime.WorkflowSessionFactory(
        model="gpt-5",
        provider="openai",
        wire_protocol="responses",
        api_key="fake",  # pragma: allowlist secret
        base_url="https://example.test",
        reasoning_effort="xhigh",
    )

    default_session = factory.build_workflow_session(prompt="analyze", budget=100_000)
    corrective_session = factory.build_workflow_session(
        prompt="commit",
        budget=100_000,
        thinking=False,
    )

    assert default_session.agent.reasoning_effort == "xhigh"
    assert default_session.agent.reasoning_effort_policy == "configured"
    assert corrective_session.agent.reasoning_effort is None
    assert corrective_session.agent.reasoning_effort_policy == "suppressed"


def test_deepseek_max_reasoning_survives_workflow_thinking_override(monkeypatch):
    _patch_build_session(monkeypatch)
    factory = workflow_runtime.WorkflowSessionFactory(
        model="deepseek-v4-flash-0731",
        provider="openai",
        wire_protocol="responses",
        api_key="fake",  # pragma: allowlist secret
        base_url="https://example.test",
        thinking=True,
        reasoning_effort="max",
    )

    session = factory.build_workflow_session(
        prompt="return structured evidence",
        budget=100_000,
        thinking=False,
    )

    assert session.agent.thinking is True
    assert session.agent.reasoning_effort == "max"
    assert session.agent.reasoning_effort_policy == "configured"


@pytest.mark.asyncio
async def test_built_context_injects_sampling_and_output_limits(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(temperature=1.0, top_p=1.0, max_output_tokens=32_768))

    await ctx.agent("solve")

    agent = calls[0]["agent"]
    assert agent.temperature == 1.0
    assert agent.top_p == 1.0
    assert agent.max_tokens_per_step == 32_768


@pytest.mark.asyncio
async def test_built_context_preserves_explicit_empty_thinking_params(monkeypatch):
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(thinking=True, thinking_params={}))

    await ctx.agent("solve")

    assert calls[0]["agent"].thinking_params == {}


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
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(model=model, provider="openai", thinking=True))

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
async def test_run_workflow_threads_independent_concurrency_caps(monkeypatch):
    _patch_build_session(monkeypatch)
    seen: dict[str, int] = {}

    async def fn(ctx, _args):
        seen["agent"] = ctx._max_concurrency
        seen["task"] = ctx._task_concurrency
        return "ok"

    result = await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        max_concurrency=2,
        task_concurrency=5,
    )

    assert result == "ok"
    assert seen == {"agent": 2, "task": 5}


@pytest.mark.asyncio
async def test_run_workflow_aggregates_session_metrics(monkeypatch):
    from workflow_runtime_test_support import _FakeSession

    values = ((5, 2, 1), (7, 3, 2))

    def fake_build_session(*, agent, **_kwargs):
        session = _FakeSession(agent, agent.tools)
        session.used_tokens, session.step_count, session.markup_recovered = values[fake_build_session.calls]
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

    result = await workflow_runtime.run_workflow(fn.__workflow_spec__, {}, cfg=_cfg())
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
@pytest.mark.parametrize("composition", ("parallel", "pipeline"))
async def test_run_workflow_marks_collection_budget_exhaustion_stopped(
    monkeypatch,
    composition,
):
    _patch_build_session(monkeypatch)

    async def fn(ctx, args):
        if composition == "parallel":
            return await ctx.parallel([lambda: ctx.agent("exhausted")])

        async def agent_stage(_previous, _item, _index):
            return await ctx.agent("exhausted")

        return await ctx.pipeline(["item"], agent_stage)

    details = await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(budget=0),
        return_details=True,
    )

    assert details.stop_reason == "budget_exceeded"
    assert details.output["status"] == "budget_exceeded"


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
async def test_every_workflow_agent_gets_its_own_id(monkeypatch):
    """Each workflow session is built with a distinct, real ``aid``.

    ``build_session`` defaults to ``aid=-1``. Left at that default every agent
    in a workflow run stamps the same ``-1`` onto its ``tool_exec`` and
    ``llm_call`` records, so the trajectory cannot be attributed per agent.
    Ids are allocated whether or not a run folder is configured.
    """
    calls = _patch_build_session(monkeypatch)
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg())

    await ctx.agent("one")
    await ctx.agent("two")
    await ctx.agent("three")

    assert [c["aid"] for c in calls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_agent_id_and_transcript_prefix_name_the_same_agent(monkeypatch, tmp_path):
    """The ``aid`` in the trace and the transcript's filename prefix agree.

    That shared number is the join key between a run folder's per-agent
    transcript and the trajectory records the agent produced.
    """
    calls = _patch_build_session(monkeypatch)
    save_dir = str(tmp_path / "run")
    ctx = workflow_runtime.build_workflow_context(cfg=_cfg(), save_dir=save_dir)

    await ctx.agent("analyze the bug", label="analyst")
    await ctx.agent("write the fix", label="coder")

    for call in calls:
        prefix = os.path.basename(call["auto_save_path"]).split("_")[0]
        assert int(prefix) == call["aid"]


def test_workflow_agent_id_reaches_the_built_session():
    """The id survives ``build_session`` into ``session.state.aid``.

    Driven through the real factory rather than a captured kwarg, so a fix that
    passes ``aid`` but loses it in the wiring still fails here.
    """
    factory = workflow_runtime.WorkflowSessionFactory(
        model="test-model",
        provider="anthropic",
        api_key="fake",  # pragma: allowlist secret
        base_url="https://example.test",
    )

    first = factory.build_workflow_session(prompt="analyze", budget=100_000)
    second = factory.build_workflow_session(prompt="fix", budget=100_000)

    assert [first.state.aid, second.state.aid] == [0, 1]


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
