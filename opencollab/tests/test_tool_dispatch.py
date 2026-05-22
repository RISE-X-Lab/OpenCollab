"""Contract checks for inline tool dispatch in ToolExecutionUseCase.

The old ``execute_tool_with_runtime`` indirection was removed; the use case
now calls ``tool.execute_with_runtime(args, runtime)`` directly. These tests
guard the runtime wiring (env / safety / permission policy passed to tools)
and the layer boundary of ``tool_execution.py`` (the home of ``ToolRuntime``).
"""

import asyncio
from pathlib import Path

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
