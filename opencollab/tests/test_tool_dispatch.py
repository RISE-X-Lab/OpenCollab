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


class LegacyOnlyTool:
    def __init__(self):
        self.legacy_calls = []

    async def execute(self, params, env=None, interceptor=None, confirm_fn=None):
        self.legacy_calls.append(
            {
                "params": params,
                "env": env,
                "interceptor": interceptor,
                "confirm_fn": confirm_fn,
            }
        )
        return "legacy result"


def test_execute_tool_with_runtime_uses_runtime_native_tool():
    runtime = ToolRuntime(environment=FakeEnv(), safety_policy=FakeSafetyPolicy(), permission_policy=None)
    tool = RuntimeNativeTool()

    result = run(execute_tool_with_runtime(tool, {"value": 1}, runtime))

    assert result == "runtime result"
    assert tool.runtime_calls == [({"value": 1}, runtime)]


def test_execute_tool_with_runtime_preserves_legacy_fallback():
    env = FakeEnv()
    safety_policy = FakeSafetyPolicy()
    permission_policy = FakePermissionPolicy()
    runtime = ToolRuntime(
        environment=env,
        safety_policy=safety_policy,
        permission_policy=permission_policy,
    )
    tool = LegacyOnlyTool()

    result = run(execute_tool_with_runtime(tool, {"value": 1}, runtime))

    assert result == "legacy result"
    assert tool.legacy_calls == [
        {
            "params": {"value": 1},
            "env": env,
            "interceptor": safety_policy,
            "confirm_fn": permission_policy.confirm,
        }
    ]


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
