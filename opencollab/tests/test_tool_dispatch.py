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
from opencollab.application.tool_execution import ToolExecutionUseCase, ToolRuntime
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
