"""Bounded tool cancellation and late-write cleanup tests.

These lifecycle tests are kept separate from the core tool-use-case contract so
the contract module remains small and focused.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_tool_execution_use_case import (
    FakeAgent,
    RuntimeNativeTool,
    build_use_case,
)

import opencollab.application.tool_execution_runtime as tool_execution_runtime
from opencollab.adapters.env import Environment


class RevocableEnvironment(Environment):
    def __init__(self):
        self._aborted = False
        self.writes = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self._ensure_active()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        return ""

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        self.writes.append((path, content))


class StubbornLateWriteTool:
    name = "stubborn_late_writer"
    default_timeout = 0.005
    disable_outer_timeout = False

    def __init__(self):
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.write_blocked = asyncio.Event()

    async def execute_with_runtime(self, args, runtime):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            try:
                await runtime.environment.write_file("late.py", "late")
            except RuntimeError:
                self.write_blocked.set()
            return "late completion"


def _bounded_tool_use_case(tool, environment):
    return build_use_case(
        agent=FakeAgent(tools=[tool]),
        environment=environment,
        cancellation_cleanup_timeout=0.01,
        cancellation_force_timeout=0.01,
        environment_abort_timeout=0.01,
    )[0]


@pytest.mark.asyncio
async def test_stubborn_timed_out_tool_cannot_write_after_execute_returns(monkeypatch):
    monkeypatch.setattr(tool_execution_runtime, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)
    environment = RevocableEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)

    started = time.monotonic()
    output, _latency = await use_case.execute_tool(tool, {})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert "Tool cancellation cleanup failed" in output
    assert "environment was revoked" in output
    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1

    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_stubborn_environment_abort_is_bounded_and_exposed(monkeypatch):
    monkeypatch.setattr(tool_execution_runtime, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)

    class StubbornAbortEnvironment(RevocableEnvironment):
        def __init__(self):
            super().__init__()
            self.abort_started = asyncio.Event()
            self.abort_release = asyncio.Event()

        async def abort(self) -> None:
            self.abort_started.set()
            while not self.abort_release.is_set():
                try:
                    await self.abort_release.wait()
                except asyncio.CancelledError:
                    continue

    environment = StubbornAbortEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)

    started = time.monotonic()
    output, _latency = await use_case.execute_tool(tool, {})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert environment.abort_started.is_set()
    assert "environment abort did not quiesce within its bounded timeout" in output
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 2

    tool.release.set()
    environment.abort_release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_cooperative_tool_timeout_keeps_existing_result_shape(monkeypatch):
    monkeypatch.setattr(tool_execution_runtime, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)

    class CooperativeTimeoutTool:
        name = "cooperative"
        default_timeout = 0.005
        disable_outer_timeout = False

        async def execute_with_runtime(self, args, runtime):
            await asyncio.Event().wait()

    tool = CooperativeTimeoutTool()
    environment = RevocableEnvironment()
    use_case = _bounded_tool_use_case(tool, environment)

    output, _latency = await use_case.execute_tool(tool, {})

    assert output.startswith("Tool execution timed out after ")
    assert "while running 'cooperative'." in output
    assert "cleanup failed" not in output
    assert environment._aborted is False
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_execute_tool_success_path_is_unchanged():
    tool = RuntimeNativeTool(output="success")
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))

    output, _latency = await use_case.execute_tool(tool, {"value": 1})

    assert output == "success"
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_execute_tool_preserves_caller_cancellation():
    class CallerCancelledTool:
        name = "caller_cancelled"
        default_timeout = None
        disable_outer_timeout = True

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def execute_with_runtime(self, args, runtime):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    tool = CallerCancelledTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))
    await tool.started.wait()

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    await asyncio.wait_for(tool.cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_repeated_caller_cancel_cannot_interrupt_late_write_cleanup():
    class CallerStubbornLateWriteTool(StubbornLateWriteTool):
        default_timeout = None
        disable_outer_timeout = True

    environment = RevocableEnvironment()
    tool = CallerStubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))
    await tool.started.wait()

    execution.cancel()
    await tool.cancel_seen.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1
    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_caller_cancel_cannot_interrupt_tool_timeout_cleanup(monkeypatch):
    monkeypatch.setattr(tool_execution_runtime, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)
    environment = RevocableEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))

    await tool.cancel_seen.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1
    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_simultaneous_outer_and_cleanup_cancel_cannot_spin():
    use_case, _ = build_use_case()
    cleanup_started = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await asyncio.Event().wait()

    cleanup_task = asyncio.create_task(cleanup())
    owner = asyncio.create_task(
        use_case._await_owned_cleanup_despite_cancellation(cleanup_task)
    )
    await cleanup_started.wait()

    owner.cancel()
    cleanup_task.cancel()

    await asyncio.wait_for(owner, timeout=0.2)
    assert cleanup_task.cancelled() is True


@pytest.mark.parametrize(
    "field",
    [
        "cancellation_cleanup_timeout",
        "cancellation_force_timeout",
        "environment_abort_timeout",
    ],
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "invalid"])
def test_tool_cleanup_timeouts_must_be_finite_and_positive(field, value):
    kwargs = {field: value}

    with pytest.raises(ValueError, match="must be a finite positive number"):
        build_use_case(**kwargs)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "invalid"])
def test_invalid_requested_tool_timeout_falls_back_to_positive_bound(value):
    tool = SimpleNamespace(default_timeout=None, disable_outer_timeout=False)
    use_case, _ = build_use_case()

    timeout = use_case.tool_execution_timeout(tool, {"timeout": value})

    assert timeout is not None
    assert math.isfinite(timeout)
    assert timeout > 0


def test_application_tool_execution_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/tool_execution.py").read_text(encoding="utf-8")

    assert "opencollab.core.session" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tui" not in source
