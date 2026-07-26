"""Observation and event sink tests for WorkflowContext."""

from __future__ import annotations

from typing import Any

import pytest
from workflow_context_test_support import (
    FakeFactory,
    FakeProbe,
    FakeSession,
    RecordingSink,
)

from opencollab.application.workflow import (
    WorkflowContext,
)


@pytest.mark.asyncio
async def test_phase_and_log_emit_via_sink():
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([]), event_sink=sink)

    await ctx.phase("planning")
    await ctx.log("hello world")

    assert len(sink.events) == 2

@pytest.mark.asyncio
async def test_phase_and_log_noop_without_sink():
    ctx = WorkflowContext(FakeFactory([]))
    # Must not raise when no sink is wired.
    await ctx.phase("planning")
    await ctx.log("hello world")

@pytest.mark.asyncio
async def test_phase_and_log_ignore_observer_failures():
    class FailingSink:
        async def emit(self, event: Any) -> None:
            raise RuntimeError("sink failed")

    class FailingTracer:
        def log_step(self, **kwargs: Any) -> None:
            raise RuntimeError("trace failed")

    ctx = WorkflowContext(
        FakeFactory([]), event_sink=FailingSink(), tracer=FailingTracer()
    )

    await ctx.phase("planning")
    await ctx.log("still running")

@pytest.mark.asyncio
async def test_tree_changed_is_none_without_probe():
    # No probe wired -> "cannot verify" -> None (callers must not hard-block).
    ctx = WorkflowContext(FakeFactory([]))
    assert await ctx.tree_changed() is None

@pytest.mark.asyncio
async def test_tree_changed_proxies_probe_result():
    ctx_yes = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(changed=True))
    ctx_no = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(changed=False))
    assert await ctx_yes.tree_changed() is True
    assert await ctx_no.tree_changed() is False

@pytest.mark.asyncio
async def test_tree_changed_swallows_probe_error_to_none():
    # A flaky git call must never abort the run: error -> None.
    ctx = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(boom=True))
    assert await ctx.tree_changed() is None

@pytest.mark.asyncio
async def test_source_changed_excludes_injected_paths():
    # The whole tree is dirty (changed=True) only because the harness git-applied
    # an injected test; with that path excluded the SOURCE is clean -> False, while
    # tree_changed still reports True. This is the core of Bug A.
    probe = FakeProbe(changed=True, excluded_changed=False)
    ctx = WorkflowContext(FakeFactory([]), tree_probe=probe)

    assert await ctx.tree_changed() is True
    assert await ctx.source_changed(["t/test_x.py"]) is False
    assert probe.exclude_calls == [("t/test_x.py",)]

@pytest.mark.asyncio
async def test_source_changed_is_none_without_probe():
    # No probe wired -> "cannot verify" -> None (callers must not hard-block).
    ctx = WorkflowContext(FakeFactory([]))
    assert await ctx.source_changed(["t/test_x.py"]) is None

@pytest.mark.asyncio
async def test_source_changed_swallows_probe_error_to_none():
    # A flaky git call must never abort the run: probe error -> None.
    ctx = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(boom=True))
    assert await ctx.source_changed(["t/test_x.py"]) is None


@pytest.mark.asyncio
async def test_diff_reports_probe_output_or_unknown() -> None:
    assert await WorkflowContext(FakeFactory([])).diff() is None
    assert (
        await WorkflowContext(
            FakeFactory([]),
            tree_probe=FakeProbe(),
        ).diff()
        == "diff"
    )
    assert (
        await WorkflowContext(
            FakeFactory([]),
            tree_probe=FakeProbe(boom=True),
        ).diff()
        is None
    )


@pytest.mark.asyncio
async def test_token_observation_reports_live_session_usage() -> None:
    ctx = WorkflowContext(
        FakeFactory([FakeSession(tokens=12)]),
        budget_total=100,
    )

    assert ctx.tokens_spent() == 0
    assert ctx.tokens_remaining() == 100
    await ctx.agent("measure usage")
    assert ctx.tokens_spent() == 12
    assert ctx.tokens_remaining() == 88
