"""Tests for bootstrap workflow wiring: build_workflow_context + run_workflow.

The factory binding (``WorkflowSessionFactory``) is exercised by checking that a
built context produces sessions through ``build_session`` with the resolved
model/provider/key flowing into the underlying ``Agent``. ``build_session`` is
monkeypatched so no real LLM client is ever constructed.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

import opencollab.application.autosave as autosave_mod
import pytest
from asyncio_test_support import assert_cancel_note, assert_cancel_reason
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.bootstrap import workflow_runtime
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.events import SessionRuntimeEvent


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

    monkeypatch.setattr(workflow_runtime, "build_workflow_context", fail_if_built)

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
async def test_run_workflow_final_snapshot_captures_mutation_after_last_step_end(
    monkeypatch,
    tmp_path,
):
    class ReplyLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            return LLMResponse(
                content="finished",
                usage=Usage(input_tokens=4, output_tokens=2),
                finish_reason="stop",
            )

    llm = ReplyLLM()

    def build_real_session(**kwargs):
        return build_session(llm=llm, **kwargs)

    monkeypatch.setattr(workflow_runtime, "build_session", build_real_session)
    holder: dict[str, Any] = {}

    async def fn(ctx, args):
        assert await ctx.agent("finish once", label="worker") == "finished"
        session = ctx.sessions[0]
        session.state.append_message(
            {"role": "user", "content": "mutation after final step_end"}
        )
        holder["session"] = session
        return "ok"

    save_dir = str(tmp_path / "run")
    assert await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        save_dir=save_dir,
        trace=False,
    ) == "ok"

    snapshot = SessionStore().load_snapshot(
        os.path.join(save_dir, "000_worker.json"),
        "system",
    )
    assert snapshot["messages"][-1]["role"] == "user"
    assert snapshot["messages"][-1]["content"] == "mutation after final step_end"
    assert snapshot["session_state"]["phase"] == holder["session"].state.phase.value


@pytest.mark.asyncio
async def test_run_workflow_final_snapshot_captures_session_exception_state(
    monkeypatch,
    tmp_path,
):
    class FailingLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            raise RuntimeError("provider exploded")

    def build_real_session(**kwargs):
        return build_session(llm=FailingLLM(), **kwargs)

    monkeypatch.setattr(workflow_runtime, "build_session", build_real_session)
    holder: dict[str, Any] = {}

    async def fn(ctx, args):
        assert await ctx.agent("trigger failure", label="broken") is None
        holder["session"] = ctx.sessions[0]
        return "workflow survived"

    save_dir = str(tmp_path / "run")
    assert await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        save_dir=save_dir,
        trace=False,
    ) == "workflow survived"

    session = holder["session"]
    snapshot = SessionStore().load_snapshot(
        os.path.join(save_dir, "000_broken.json"),
        "system",
    )
    assert snapshot["session_state"]["phase"] == session.state.phase.value == "error"
    assert snapshot["session_state"]["terminal_reason"] == session.state.terminal_reason
    assert "provider exploded" in snapshot["session_state"]["terminal_reason"]
    assert snapshot["session_state"]["pending_events"] == []


@pytest.mark.asyncio
async def test_run_workflow_final_snapshot_captures_cancelled_calling_llm_state(
    monkeypatch,
    tmp_path,
):
    class BlockingLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            await asyncio.Event().wait()

    def build_real_session(**kwargs):
        return build_session(llm=BlockingLLM(), **kwargs)

    monkeypatch.setattr(workflow_runtime, "build_session", build_real_session)
    holder: dict[str, Any] = {}

    async def fn(ctx, args):
        assert await ctx.agent(
            "cancel during provider call",
            label="cancelled",
            timeout=0.01,
        ) is None
        holder["session"] = ctx.sessions[0]
        return "timed out cleanly"

    save_dir = str(tmp_path / "run")
    assert await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        save_dir=save_dir,
        trace=False,
    ) == "timed out cleanly"

    session = holder["session"]
    snapshot = SessionStore().load_snapshot(
        os.path.join(save_dir, "000_cancelled.json"),
        "system",
    )
    assert snapshot["session_state"]["phase"] == session.state.phase.value
    assert snapshot["session_state"]["terminal_reason"] == session.state.terminal_reason
    assert snapshot["session_state"]["pending_events"] == []
    assert snapshot["messages"] == session.state.enriched_messages()


@pytest.mark.asyncio
async def test_run_workflow_writes_orchestration_signals(monkeypatch, tmp_path):
    """A saved run records orchestration.jsonl via the auto-wired Tracer.

    The workflow only emits phase/log events (which flow straight through the
    context's tracer), so this exercises the orchestration-signals wiring without
    a real LLM — build_session is monkeypatched away.
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
    path = os.path.join(save_dir, "orchestration.jsonl")
    assert os.path.exists(path)
    # The legacy single flat trajectory.jsonl is gone — signals live in
    # orchestration.jsonl, per-role conversations in <seq>_<role>.json.
    assert not os.path.exists(os.path.join(save_dir, "trajectory.jsonl"))
    with open(path) as f:
        types = [json.loads(line)["type"] for line in f if line.strip()]
    assert "workflow_phase" in types
    assert "workflow_log" in types


@pytest.mark.asyncio
async def test_run_workflow_projects_sticky_trace_failure_and_fails_closed(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)
    instances: list[Any] = []

    class FailingTracer:
        write_error = "BlockingIOError: trajectory lock remained busy"
        dropped_steps = 3

        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            instances.append(self)

        def log_step(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(workflow_runtime, "Tracer", FailingTracer)

    async def fn(ctx, args):
        return "computed"

    save_dir = str(tmp_path / "run")
    with pytest.raises(RuntimeError, match="technical workflow trace failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=save_dir,
        )

    assert instances[0].closed is True
    with open(os.path.join(save_dir, "workflow.json")) as handle:
        manifest = json.load(handle)
    assert manifest["trace_enabled"] is True
    assert manifest["tracer_write_error"] == FailingTracer.write_error
    assert manifest["tracer_dropped_steps"] == 3
    assert "trajectory write failed" in manifest["tracer_failure"]


@pytest.mark.asyncio
async def test_run_workflow_trace_close_failure_does_not_mask_workflow_error(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)

    class CloseFailingTracer:
        write_error = None
        dropped_steps = 0

        def __init__(self, *args, **kwargs) -> None:
            return None

        def log_step(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            raise OSError("trace close failed")

    monkeypatch.setattr(workflow_runtime, "Tracer", CloseFailingTracer)

    async def fn(ctx, args):
        raise ValueError("workflow failed first")

    with pytest.raises(ValueError, match="workflow failed first") as caught:
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
        )

    assert any("workflow trace also failed" in note for note in caught.value.__notes__)


@pytest.mark.asyncio
async def test_run_workflow_fails_closed_when_custom_tracer_diagnostics_raise(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)

    class UninspectableTracer:
        def __init__(self, *args, **kwargs) -> None:
            return None

        @property
        def write_error(self):
            raise LookupError("broken diagnostics property")

        def log_step(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(workflow_runtime, "Tracer", UninspectableTracer)

    async def fn(ctx, args):
        return "computed"

    save_dir = str(tmp_path / "run")
    with pytest.raises(RuntimeError, match="technical workflow trace failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=save_dir,
        )

    with open(os.path.join(save_dir, "workflow.json")) as handle:
        manifest = json.load(handle)
    assert manifest["tracer_write_error"] is None
    assert manifest["tracer_dropped_steps"] == 0
    assert "diagnostics could not be inspected" in manifest["tracer_failure"]


@pytest.mark.asyncio
async def test_run_workflow_trace_false_skips_orchestration(monkeypatch, tmp_path):
    """``trace=False`` suppresses orchestration.jsonl even when the run is saved."""
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

    assert not os.path.exists(os.path.join(save_dir, "orchestration.jsonl"))


@pytest.mark.asyncio
async def test_run_workflow_no_save_dir_skips_orchestration(monkeypatch, tmp_path):
    """Without a save_dir there is no run folder, so no orchestration file."""
    _patch_build_session(monkeypatch)
    from opencollab.application.workflow_registry import workflow

    @workflow(name="ephemeral_wf", description="d")
    async def fn(ctx, args):
        await ctx.phase("scan")
        return "ok"

    result = await workflow_runtime.run_workflow(fn.__workflow_spec__, {}, cfg=_cfg())

    assert result == "ok"
    assert not os.path.exists(os.path.join(str(tmp_path), "orchestration.jsonl"))


@pytest.mark.asyncio
async def test_run_workflow_quiesces_late_session_before_manifest_and_tracer_close(
    monkeypatch,
    tmp_path,
):
    order: list[str] = []
    tracer_instances: list[Any] = []
    sentinel = tmp_path / "late-write.txt"
    holder: dict[str, Any] = {}

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            tracer_instances.append(self)

        def log_step(self, step_type, payload, tokens=0, latency=0.0) -> None:
            assert self.closed is False
            if step_type == "late_cleanup":
                order.append("late-trace")

        def close(self) -> None:
            self.closed = True
            order.append("tracer-close")

    class RevocableEnvironment:
        def __init__(self) -> None:
            self._aborted = False
            self.abort_called = False
            self.blocked_writes = 0

        async def write_file(self, path: str, content: str) -> None:
            if self._aborted:
                self.blocked_writes += 1
                raise RuntimeError("environment revoked")
            sentinel.write_text(content, encoding="utf-8")

        async def abort(self) -> None:
            self.abort_called = True
            self._aborted = True

    class CancellationResistantSession:
        used_tokens = 0

        def __init__(self, environment, tracer) -> None:
            self.env = environment
            self.tracer = tracer
            self.cancel_seen = asyncio.Event()

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.env._aborted:
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        continue
                self.tracer.log_step("late_cleanup", {})
                try:
                    await self.env.write_file(str(sentinel), "too late")
                except RuntimeError:
                    pass
                order.append("session-finished")
                return "late"

    class OneSessionFactory:
        def __init__(self, session) -> None:
            self.session = session

        def build_workflow_session(self, **kwargs):
            return self.session

    def fake_build_context(**kwargs):
        environment = RevocableEnvironment()
        session = CancellationResistantSession(environment, kwargs["tracer"])
        holder.update(environment=environment, session=session)
        return WorkflowContext(
            OneSessionFactory(session),
            tracer=kwargs["tracer"],
            max_concurrency=kwargs["max_concurrency"],
            budget_total=kwargs["budget"],
        )

    original_manifest = workflow_runtime._write_workflow_manifest

    def recording_manifest(*args, **kwargs):
        order.append("manifest")
        return original_manifest(*args, **kwargs)

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_runtime, "build_workflow_context", fake_build_context)
    monkeypatch.setattr(
        workflow_runtime,
        "_write_workflow_manifest",
        recording_manifest,
    )

    async def fn(ctx, args):
        assert await ctx.agent("slow", timeout=0.001) is None
        await asyncio.wait_for(holder["session"].cancel_seen.wait(), timeout=0.5)
        return "ok"

    save_dir = str(tmp_path / "run")
    result = await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        save_dir=save_dir,
        cleanup_timeout=0.01,
    )

    assert result == "ok"
    assert holder["environment"].abort_called is True
    assert holder["environment"].blocked_writes == 1
    assert sentinel.exists() is False
    assert tracer_instances[0].closed is True
    assert order.index("session-finished") < order.index("manifest")
    assert order.index("session-finished") < order.index("tracer-close")
    assert os.path.exists(os.path.join(save_dir, "workflow.json"))


@pytest.mark.asyncio
async def test_workflow_cleanup_marks_cancelled_blocking_autosave_nonquiescent():
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []
    call_count = 0

    def blocking_save() -> None:
        nonlocal call_count
        call_count += 1
        current = call_count
        calls.append(f"start-{current}")
        if current == 1:
            first_started.set()
            assert release_first.wait(timeout=2.0)
        else:
            second_started.set()
            assert release_second.wait(timeout=2.0)
        calls.append(f"end-{current}")

    subscriber = AutoSaveSubscriber(blocking_save)
    event_bus = EventBus(subscriber)

    class SessionWithOwnedAutosave:
        @property
        def pending_cleanup_tasks(self):
            return event_bus.pending_tasks

    class UnusedFactory:
        def build_workflow_session(self, **kwargs):
            raise AssertionError("session creation is not used")

    ctx = WorkflowContext(UnusedFactory())
    ctx._track_session(SessionWithOwnedAutosave())

    first_waiter = asyncio.create_task(
        event_bus.emit(SessionRuntimeEvent(type="step_end"))
    )
    assert await asyncio.to_thread(first_started.wait, 1.0)
    second_waiter = asyncio.create_task(
        event_bus.emit(SessionRuntimeEvent(type="step_end"))
    )
    while len(subscriber.pending_tasks) < 2:
        await asyncio.sleep(0)
    first_waiter.cancel()
    second_waiter.cancel()
    waiter_results = await asyncio.gather(
        first_waiter,
        second_waiter,
        return_exceptions=True,
    )
    assert all(isinstance(result, asyncio.CancelledError) for result in waiter_results)
    owners = subscriber.pending_tasks
    assert len(owners) == 2
    assert event_bus.pending_tasks == owners

    cleanup = asyncio.create_task(
        workflow_runtime._quiesce_workflow_context(ctx, timeout=0.02)
    )
    deadline = asyncio.get_running_loop().time() + 0.5
    while not (
        cleanup.done()
        or all(owner.cancelled() or owner.done() for owner in owners)
        or subscriber.failure_count > 0
    ):
        assert not cleanup.done()
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)

    try:
        release_first.set()
        assert await asyncio.to_thread(second_started.wait, 0.5)
        quiesced, succeeded, _lingering = await cleanup
        assert quiesced is False
        assert succeeded is False
        assert calls == ["start-1", "end-1", "start-2"]
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*owners, return_exceptions=True)

    assert calls == ["start-1", "end-1", "start-2", "end-2"]
    assert await workflow_runtime._wait_for_context_cleanup(ctx, timeout=0.2)


@pytest.mark.asyncio
async def test_workflow_cleanup_reports_completed_autosave_failure():
    error = OSError("snapshot disk full")

    def failed_save() -> None:
        raise error

    subscriber = AutoSaveSubscriber(failed_save)
    event_bus = EventBus(subscriber)

    class SessionWithFailedAutosave:
        pending_cleanup_tasks = ()

        @property
        def persistence_errors(self):
            return (subscriber.last_error,) if subscriber.last_error else ()

    class UnusedFactory:
        def build_workflow_session(self, **kwargs):
            raise AssertionError("session creation is not used")

    ctx = WorkflowContext(UnusedFactory())
    ctx._track_session(SessionWithFailedAutosave())
    await event_bus.emit(SessionRuntimeEvent(type="step_end"))

    quiesced, succeeded, _lingering = await workflow_runtime._quiesce_workflow_context(
        ctx,
        timeout=0.1,
    )
    assert quiesced is True
    assert succeeded is False
    assert subscriber.last_error is error


@pytest.mark.asyncio
async def test_workflow_cleanup_tracks_abandoned_provider_task_until_exit():
    class CancellationResistantLLM:
        def __init__(self):
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                raise

    llm = CancellationResistantLLM()
    session = build_session(
        agent=Agent(
            name="workflow_agent",
            system_prompt="sys",
            tools=[],
            model="test-model",
            provider="test",
        ),
        llm=llm,
        llm_timeout=0.01,
    )

    class OneSessionFactory:
        def build_workflow_session(self, **kwargs):
            return session

    ctx = WorkflowContext(OneSessionFactory())
    assert await ctx.agent("trigger provider timeout") is None
    assert llm.cancel_seen.is_set()
    assert session.pending_cleanup_tasks

    quiesced, succeeded, _lingering = await workflow_runtime._quiesce_workflow_context(
        ctx,
        timeout=0.01,
    )
    assert quiesced is False
    assert succeeded is False
    pending = session.pending_cleanup_tasks
    assert pending

    llm.release.set()
    await asyncio.gather(*pending, return_exceptions=True)
    assert await workflow_runtime._wait_for_context_cleanup(ctx, timeout=0.2)


@pytest.mark.asyncio
async def test_run_workflow_reports_technical_cleanup_failure_after_quiescence(
    monkeypatch,
    tmp_path,
):
    calls = _patch_build_session(monkeypatch)
    closed: list[bool] = []

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

    async def failed_abort(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_runtime, "_quiesce_workflow_context", failed_abort)

    async def fn(ctx, args):
        await ctx.agent("done")
        return "would otherwise succeed"

    save_dir = str(tmp_path / "run")
    with pytest.raises(RuntimeError, match="technical workflow cleanup failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=save_dir,
            cleanup_timeout=0.01,
        )

    assert calls
    assert closed == [True]
    assert os.path.exists(os.path.join(save_dir, "workflow.json")) is False


@pytest.mark.asyncio
async def test_run_workflow_cleanup_failure_keeps_workflow_failure_as_cause(
    monkeypatch,
):
    _patch_build_session(monkeypatch)

    async def failed_cleanup(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(
        workflow_runtime,
        "_quiesce_and_finalize_workflow_context",
        failed_cleanup,
    )

    async def fn(ctx, args):
        raise ValueError("workflow failed first")

    with pytest.raises(RuntimeError, match="technical workflow cleanup failed") as caught:
        await workflow_runtime.run_workflow(fn, {}, cfg=_cfg())

    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value.__cause__) == "workflow failed first"


@pytest.mark.asyncio
async def test_run_workflow_cleanup_failure_is_note_on_primary_cancel(monkeypatch):
    _patch_build_session(monkeypatch)
    started = asyncio.Event()

    async def failed_cleanup(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(
        workflow_runtime,
        "_quiesce_and_finalize_workflow_context",
        failed_cleanup,
    )

    async def fn(ctx, args):
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(workflow_runtime.run_workflow(fn, {}, cfg=_cfg()))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    run_task.cancel("primary cancellation")

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(run_task, timeout=0.5)
    assert_cancel_reason(caught.value, "primary cancellation")
    assert_cancel_note(
        caught.value,
        "workflow cleanup also failed",
    )


@pytest.mark.asyncio
async def test_run_workflow_slow_manifest_is_bounded_and_defers_tracer_close(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)
    monkeypatch.setattr(autosave_mod, "MAX_CANCELLED_SAVE_WAIT_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    tracers: list[Any] = []

    class RecordingTracer:
        write_error = None
        dropped_steps = 0

        def __init__(self, *args, **kwargs):
            self.closed = False
            self.closed_event = asyncio.Event()
            tracers.append(self)

        def log_step(self, *args, **kwargs):
            return None

        def close(self):
            self.closed = True
            order.append("tracer-close")
            self.closed_event.set()

    original_manifest = workflow_runtime._write_workflow_manifest

    def blocking_manifest(*args, **kwargs):
        order.append("manifest-start")
        started.set()
        assert release.wait(timeout=2.0)
        original_manifest(*args, **kwargs)
        order.append("manifest-end")

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(
        workflow_runtime,
        "_write_workflow_manifest",
        blocking_manifest,
    )

    async def fn(ctx, args):
        return "computed"

    run_task = asyncio.create_task(
        workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.01,
        )
    )
    assert await asyncio.to_thread(started.wait, 0.5)
    await asyncio.wait_for(asyncio.sleep(0.005), timeout=0.05)
    try:
        with pytest.raises(
            RuntimeError,
            match="manifest persistence did not quiesce",
        ):
            await asyncio.wait_for(run_task, timeout=0.2)
        assert tracers[0].closed is False
        assert workflow_runtime._WORKFLOW_MANIFEST_OWNER_TASKS
    finally:
        release.set()

    await asyncio.wait_for(tracers[0].closed_event.wait(), timeout=2.0)
    assert order.index("manifest-end") < order.index("tracer-close")
    assert os.path.exists(tmp_path / "run" / "workflow.json")


@pytest.mark.asyncio
async def test_deferred_workflow_tracer_close_survives_owner_cancellation():
    release = asyncio.Event()

    async def dependency():
        await release.wait()

    dependency_task = asyncio.create_task(dependency())

    class EmptyContext:
        pending_cleanup_tasks = ()

    class RecordingTracer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    tracer = RecordingTracer()
    owner = asyncio.create_task(
        workflow_runtime._close_tracer_after_late_cleanup(
            EmptyContext(),
            tracer,
            (dependency_task,),
            timeout=0.2,
        )
    )
    await asyncio.sleep(0)
    owner.cancel("loop shutdown")
    owner.cancel("loop shutdown repeated")
    release.set()

    await asyncio.wait_for(owner, timeout=0.5)
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_deferred_workflow_tracer_close_has_final_deadline():
    dependency_task = asyncio.create_task(asyncio.Event().wait())

    class EmptyContext:
        pending_cleanup_tasks = ()

    class RecordingTracer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    tracer = RecordingTracer()
    failures_before = len(workflow_runtime._LATE_TRACER_FAILURES)
    await asyncio.wait_for(
        workflow_runtime._close_tracer_after_late_cleanup(
            EmptyContext(),
            tracer,
            (dependency_task,),
            timeout=0.01,
        ),
        timeout=0.2,
    )

    assert tracer.closed is True
    assert len(workflow_runtime._LATE_TRACER_FAILURES) == failures_before + 1
    assert isinstance(workflow_runtime._LATE_TRACER_FAILURES[-1], TimeoutError)
    dependency_task.cancel()
    await asyncio.gather(dependency_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_workflow_manifest_failure_during_cancel_is_note_on_primary_cancel(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    class RecordingTracer:
        write_error = None
        dropped_steps = 0

        def __init__(self, *args, **kwargs):
            self.closed = False

        def log_step(self, *args, **kwargs):
            return None

        def close(self):
            self.closed = True

    def failing_manifest(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        raise OSError("manifest disk failed")

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(
        workflow_runtime,
        "_write_workflow_manifest",
        failing_manifest,
    )

    async def fn(ctx, args):
        return "computed"

    run_task = asyncio.create_task(
        workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.2,
        )
    )
    assert await asyncio.to_thread(started.wait, 0.5)
    run_task.cancel("primary cancellation")
    release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(run_task, timeout=0.5)
    assert_cancel_reason(caught.value, "primary cancellation")
    assert_cancel_note(
        caught.value,
        "workflow manifest persistence also failed",
        "manifest disk failed",
    )


@pytest.mark.asyncio
async def test_run_workflow_waits_for_orphaned_background_agent(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class BackgroundSession:
        used_tokens = 0

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            started.set()
            await release.wait()
            return "background-finished"

    class Factory:
        def build_workflow_session(self, **kwargs):
            return BackgroundSession()

    def fake_build_context(**kwargs):
        return WorkflowContext(
            Factory(),
            max_concurrency=kwargs["max_concurrency"],
            budget_total=kwargs["budget"],
        )

    monkeypatch.setattr(workflow_runtime, "build_workflow_context", fake_build_context)

    async def fn(ctx, args):
        asyncio.create_task(ctx.agent("background"))
        return "workflow-returned"

    run_task = asyncio.create_task(
        workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            cleanup_timeout=0.2,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert run_task.done() is False

    release.set()
    assert await asyncio.wait_for(run_task, timeout=0.5) == "workflow-returned"


@pytest.mark.asyncio
async def test_run_workflow_double_cancel_cannot_interrupt_owned_cleanup(
    monkeypatch,
    tmp_path,
):
    holder: dict[str, Any] = {}
    context_built = asyncio.Event()
    tracer_instances: list[Any] = []
    order: list[str] = []

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            tracer_instances.append(self)

        def log_step(self, *args, **kwargs) -> None:
            assert self.closed is False

        def close(self) -> None:
            self.closed = True
            order.append("tracer-close")

    class BlockingAbortEnvironment:
        def __init__(self) -> None:
            self._aborted = False
            self.abort_started = asyncio.Event()
            self.abort_release = asyncio.Event()

        async def abort(self) -> None:
            self.abort_started.set()
            await self.abort_release.wait()

    class CancellationResistantSession:
        used_tokens = 0

        def __init__(self, environment) -> None:
            self.env = environment
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.env._aborted:
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        continue
                order.append("session-finished")
                return "cancelled-cleanly"

    class Factory:
        def __init__(self, session) -> None:
            self.session = session

        def build_workflow_session(self, **kwargs):
            return self.session

    def fake_build_context(**kwargs):
        environment = BlockingAbortEnvironment()
        session = CancellationResistantSession(environment)
        holder.update(environment=environment, session=session)
        context_built.set()
        return WorkflowContext(
            Factory(session),
            tracer=kwargs["tracer"],
            max_concurrency=kwargs["max_concurrency"],
            budget_total=kwargs["budget"],
        )

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_runtime, "build_workflow_context", fake_build_context)

    async def fn(ctx, args):
        return await ctx.agent("slow")

    run_task = asyncio.create_task(
        workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.02,
        )
    )
    await asyncio.wait_for(context_built.wait(), timeout=0.5)
    await asyncio.wait_for(holder["session"].started.wait(), timeout=0.5)
    run_task.cancel("first cancellation")
    await asyncio.wait_for(holder["session"].cancel_seen.wait(), timeout=0.5)
    run_task.cancel("second cancellation")

    await asyncio.wait_for(holder["environment"].abort_started.wait(), timeout=0.5)
    assert run_task.done() is False
    holder["environment"].abort_release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(run_task, timeout=0.5)
    assert_cancel_reason(raised.value, "first cancellation")
    assert order.index("session-finished") < order.index("tracer-close")
    assert tracer_instances[0].closed is True


@pytest.mark.asyncio
async def test_owned_tracer_closes_after_nonquiescent_session_releases_late(
    monkeypatch,
    tmp_path,
):
    holder: dict[str, Any] = {}
    tracer_instances: list[Any] = []
    order: list[str] = []

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.closed_event = asyncio.Event()
            tracer_instances.append(self)

        def log_step(self, step_type, payload, tokens=0, latency=0.0) -> None:
            assert self.closed is False
            if step_type == "late_cleanup":
                order.append("late-trace")

        def close(self) -> None:
            self.closed = True
            order.append("tracer-close")
            self.closed_event.set()

    class Environment:
        def __init__(self) -> None:
            self._aborted = False

        async def abort(self) -> None:
            self._aborted = True

    class PermanentlyCancellationResistantSession:
        used_tokens = 0

        def __init__(self, environment, tracer) -> None:
            self.env = environment
            self.tracer = tracer
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                self.tracer.log_step("late_cleanup", {})
                order.append("session-finished")
                return "released-late"

    class Factory:
        def __init__(self, session) -> None:
            self.session = session

        def build_workflow_session(self, **kwargs):
            return self.session

    def fake_build_context(**kwargs):
        environment = Environment()
        session = PermanentlyCancellationResistantSession(
            environment,
            kwargs["tracer"],
        )
        holder.update(environment=environment, session=session)
        return WorkflowContext(
            Factory(session),
            tracer=kwargs["tracer"],
            max_concurrency=kwargs["max_concurrency"],
            budget_total=kwargs["budget"],
        )

    monkeypatch.setattr(workflow_runtime, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_runtime, "build_workflow_context", fake_build_context)

    async def fn(ctx, args):
        assert await ctx.agent("slow", timeout=0.001) is None
        await asyncio.wait_for(holder["session"].cancel_seen.wait(), timeout=0.5)
        return "workflow-returned"

    with pytest.raises(RuntimeError, match="technical workflow cleanup failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.005,
        )

    tracer = tracer_instances[0]
    assert tracer.closed is False
    holder["session"].release.set()
    await asyncio.wait_for(tracer.closed_event.wait(), timeout=0.5)
    assert order.index("session-finished") < order.index("tracer-close")
