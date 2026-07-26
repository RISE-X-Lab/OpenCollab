"""Workflow runtime artifact and trace tests."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from workflow_runtime_test_support import (
    _cfg,
    _patch_build_session,
)

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import (
    _workflow_runtime_execution as workflow_execution,
)
from opencollab.bootstrap import (
    _workflow_runtime_session as workflow_session,
)
from opencollab.bootstrap import (
    workflow_runtime,
)
from opencollab.bootstrap.session_factory import build_session


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

    monkeypatch.setattr(workflow_session, "build_session", build_real_session)
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

    monkeypatch.setattr(workflow_session, "build_session", build_real_session)
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

    monkeypatch.setattr(workflow_session, "build_session", build_real_session)
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

    monkeypatch.setattr(workflow_execution, "Tracer", FailingTracer)

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
    assert manifest["evidence_complete"] is False

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

    monkeypatch.setattr(workflow_execution, "Tracer", CloseFailingTracer)

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

    monkeypatch.setattr(workflow_execution, "Tracer", UninspectableTracer)

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
    assert manifest["evidence_complete"] is False

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
