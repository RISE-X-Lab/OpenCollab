"""Contract checks for inline tool dispatch in ToolExecutionUseCase.

The old ``execute_tool_with_runtime`` indirection was removed; the use case
now calls ``tool.execute_with_runtime(args, runtime)`` directly. These tests
guard the runtime wiring (env / safety / permission policy passed to tools)
and the layer boundary of ``tool_execution.py`` (the home of ``ToolRuntime``).
"""

import asyncio
from pathlib import Path

import opencollab.application.tool_execution as tool_execution_mod
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.tool_execution import (
    DeferredCall,
    ToolExecutionUseCase,
    ToolRuntime,
)
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    pass


class FakeSafetyPolicy:
    pass


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


class FakeEventPublisher:
    async def emit(self, event):
        pass


class FakeAgent:
    def __init__(self, tools):
        self.tools = tools

    def find_tool(self, name):
        for t in self.tools:
            if t.name == name:
                return t
        return None


class RuntimeNativeTool:
    name = "fake_tool"

    def __init__(self):
        self.runtime_calls = []

    async def execute_with_runtime(self, params, runtime):
        self.runtime_calls.append((params, runtime))
        return "runtime result"


class HangingTool(RuntimeNativeTool):
    async def execute_with_runtime(self, params, runtime):
        self.runtime_calls.append((params, runtime))
        await asyncio.sleep(3600)
        return "unreachable"


class InternallyBoundedTool(RuntimeNativeTool):
    disable_outer_timeout = True


class DeferredTool(RuntimeNativeTool):
    async def execute_with_runtime(self, params, runtime):
        self.runtime_calls.append((params, runtime))
        return DeferredCall(ref=17)


class RecordingEventPublisher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class FailingEventPublisher(RecordingEventPublisher):
    def __init__(self, fail_type):
        super().__init__()
        self.fail_type = fail_type

    async def emit(self, event):
        self.events.append(event)
        if event.type == self.fail_type:
            raise RuntimeError(f"{event.type} sink failed")


def _tool_call(args: str = "{}") -> dict:
    return {"id": "c1", "function": {"name": "fake_tool", "arguments": args}}


def test_use_case_invokes_tool_execute_with_runtime():
    tool = RuntimeNativeTool()
    env = FakeEnv()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=env,
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
        safety_policy=FakeSafetyPolicy(),
        permission_policy=FakePermissionPolicy(),
    )

    result = run(use_case.process([_tool_call('{"value": 1}')]))

    assert result.messages_to_append[0]["content"] == "runtime result"
    params, runtime = tool.runtime_calls[0]
    assert params == {"value": 1}
    assert isinstance(runtime, ToolRuntime)
    assert runtime.environment is env


def test_use_case_times_out_hung_tool(monkeypatch):
    monkeypatch.setattr(tool_execution_mod, "DEFAULT_TOOL_EXECUTION_TIMEOUT", 0.01)
    tool = HangingTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    result = run(use_case.process([_tool_call()]))

    assert "Tool execution timed out after" in result.messages_to_append[0]["content"]
    assert "fake_tool" in result.messages_to_append[0]["content"]
    assert len(tool.runtime_calls) == 1


def test_internally_bounded_tool_bypasses_single_tool_outer_timeout(monkeypatch):
    seen_timeouts = []

    async def record_timeout(awaitable, timeout):
        seen_timeouts.append(timeout)
        return await asyncio.shield(awaitable)

    monkeypatch.setattr(
        ToolExecutionUseCase,
        "_await_execution_task",
        staticmethod(record_timeout),
    )
    tool = InternallyBoundedTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    result = run(use_case.process([_tool_call()]))

    assert result.messages_to_append[0]["content"] == "runtime result"
    assert seen_timeouts == [None]


def test_tool_internal_timeout_is_not_reported_as_outer_timeout():
    class InternallyTimingOutTool(RuntimeNativeTool):
        async def execute_with_runtime(self, params, runtime):
            raise asyncio.TimeoutError("provider deadline")

    tool = InternallyTimingOutTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    result = run(use_case.process([_tool_call()]))

    assert result.messages_to_append[0]["content"] == (
        "Tool execution error: TimeoutError: provider deadline"
    )


def test_nested_caller_timeout_is_not_reported_as_tool_outer_timeout():
    class InternallyTimingOutTool(RuntimeNativeTool):
        async def execute_with_runtime(self, params, runtime):
            raise tool_execution_mod.CallerTimeoutError("nested deadline")

    tool = InternallyTimingOutTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    result = run(use_case.process([_tool_call()]))

    assert result.messages_to_append[0]["content"] == (
        "Tool execution error: CallerTimeoutError: nested deadline"
    )


def test_deferred_tool_uses_timeout_and_always_emits_tool_end(monkeypatch):
    monkeypatch.setattr(tool_execution_mod, "DEFAULT_TOOL_EXECUTION_TIMEOUT", 0.01)
    monkeypatch.setattr(tool_execution_mod, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.0)
    tool = HangingTool()
    publisher = RecordingEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=publisher,
    )

    ref, error = run(use_case.execute_deferred(_tool_call()))

    assert ref is None
    assert "Tool execution timed out after" in error
    assert [event.type for event in publisher.events] == ["tool_start", "tool_end"]


def test_deferred_tool_preserves_deferred_result_and_emits_tool_end():
    tool = DeferredTool()
    publisher = RecordingEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=publisher,
    )

    ref, error = run(use_case.execute_deferred(_tool_call()))

    assert (ref, error) == (17, None)
    assert [event.type for event in publisher.events] == ["tool_start", "tool_end"]


def test_deferred_tool_start_event_failure_does_not_block_spawn():
    tool = DeferredTool()
    publisher = FailingEventPublisher("tool_start")
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=publisher,
    )

    ref, error = run(use_case.execute_deferred(_tool_call()))

    assert (ref, error) == (17, None)
    assert len(tool.runtime_calls) == 1


def test_deferred_tool_end_event_failure_preserves_child_reference():
    tool = DeferredTool()
    publisher = FailingEventPublisher("tool_end")
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=publisher,
    )

    ref, error = run(use_case.execute_deferred(_tool_call()))

    assert (ref, error) == (17, None)


def test_non_finite_tool_timeout_falls_back_to_default():
    tool = RuntimeNativeTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    expected = tool_execution_mod.DEFAULT_TOOL_EXECUTION_TIMEOUT + 10.0
    assert use_case.tool_execution_timeout(tool, {"timeout": "nan"}) == expected
    assert use_case.tool_execution_timeout(tool, {"timeout": "inf"}) == expected


def test_invalid_timeout_environment_values_fall_back(monkeypatch):
    for value in ("nan", "inf", "-1", "invalid"):
        monkeypatch.setenv("OPENCOLLAB_TEST_TIMEOUT", value)
        assert tool_execution_mod._positive_env_float("OPENCOLLAB_TEST_TIMEOUT", 17.0) == 17.0


def test_outer_timeout_uses_tool_default_timeout_before_framework_fallback():
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    assert use_case.tool_execution_timeout(RunTestsTool(), {}) == 310.0


def test_explicit_timeout_stays_in_tool_args():
    tool = RuntimeNativeTool()
    use_case = ToolExecutionUseCase(
        agent=FakeAgent([tool]),
        environment=FakeEnv(),
        state=SessionState(messages=[]),
        event_publisher=FakeEventPublisher(),
    )

    result = run(use_case.process([_tool_call('{"timeout": 12}')]))

    assert result.messages_to_append[0]["content"] == "runtime result"
    assert tool.runtime_calls[0][0] == {"timeout": 12}
    assert use_case.tool_execution_timeout(tool, {"timeout": 12}) == 22.0


def test_runtime_exposes_confirm_fn_from_permission_policy():
    permission = FakePermissionPolicy()
    runtime = ToolRuntime(
        environment=FakeEnv(),
        safety_policy=FakeSafetyPolicy(),
        permission_policy=permission,
    )

    assert runtime.confirm_fn() == permission.confirm


def test_tool_execution_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/tool_execution.py").read_text(encoding="utf-8")

    assert "opencollab.core" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tui" not in source
