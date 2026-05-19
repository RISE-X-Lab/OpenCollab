import asyncio
from pathlib import Path

from opencollab.application.tool_dispatch import execute_tool_with_runtime
from opencollab.application.tool_runtime import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    pass


class FakeSafetyPolicy:
    pass


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


class RuntimeNativeTool:
    def __init__(self):
        self.runtime_calls = []

    async def execute_with_runtime(self, params, runtime):
        self.runtime_calls.append((params, runtime))
        return "runtime result"


def test_execute_tool_with_runtime_calls_execute_with_runtime():
    runtime = ToolRuntime(environment=FakeEnv(), safety_policy=FakeSafetyPolicy(), permission_policy=None)
    tool = RuntimeNativeTool()

    result = run(execute_tool_with_runtime(tool, {"value": 1}, runtime))

    assert result == "runtime result"
    assert tool.runtime_calls == [({"value": 1}, runtime)]


def test_execute_tool_with_runtime_passes_permission_policy_in_runtime():
    permission_policy = FakePermissionPolicy()
    runtime = ToolRuntime(
        environment=FakeEnv(),
        safety_policy=FakeSafetyPolicy(),
        permission_policy=permission_policy,
    )
    tool = RuntimeNativeTool()

    run(execute_tool_with_runtime(tool, {"value": 1}, runtime))

    _params, received_runtime = tool.runtime_calls[0]
    assert received_runtime.permission_policy is permission_policy
    assert received_runtime.confirm_fn() == permission_policy.confirm


def test_application_tool_runtime_modules_do_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    app_files = [
        package_root / "opencollab/application/tool_dispatch.py",
        package_root / "opencollab/application/tool_runtime.py",
    ]

    for path in app_files:
        source = path.read_text(encoding="utf-8")
        assert "opencollab.core" not in source
        assert "opencollab.tools" not in source
        assert "opencollab.bootstrap" not in source
        assert "opencollab.tui" not in source
