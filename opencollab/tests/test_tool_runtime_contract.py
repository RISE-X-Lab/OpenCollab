import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab.application.tool_runtime import ToolRuntime
from opencollab.core.env import LocalEnvironment
from opencollab.tools.bash import BashTool
from opencollab.tools.base import Tool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.tools.human import AskUserTool
from opencollab.tools.safety import SandboxInterceptor


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")

    async def read_file(self, path: str) -> str:
        raise AssertionError("read_file was not expected")

    async def write_file(self, path: str, content: str) -> None:
        raise AssertionError("write_file was not expected")


class SpySafetyPolicy:
    def __init__(self):
        self.cmd_calls = []
        self.path_calls = []

    def check_path(self, target_path: str) -> str:
        self.path_calls.append(target_path)
        return target_path

    def check_cmd(self, cmd: str) -> None:
        pass

    def is_risky(self, cmd: str) -> bool:
        return False

    async def check_cmd_interactive(self, cmd: str, confirm_fn=None) -> None:
        self.cmd_calls.append((cmd, confirm_fn))


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


class LegacyEchoTool(Tool):
    async def execute(self, params, env=None, interceptor=None, confirm_fn=None):
        if interceptor:
            await interceptor.check_cmd_interactive(params["command"], confirm_fn)
        result = await env.exec_cmd(params["command"])
        return result.stdout


def test_bash_tool_without_env_returns_existing_error():
    result = run(BashTool().execute({"command": "pwd"}))

    assert result == "Error: no execution environment available."


def test_bash_tool_execute_with_runtime_without_env_returns_existing_error():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(BashTool().execute_with_runtime({"command": "pwd"}, runtime))

    assert result == "Error: no execution environment available."


def test_bash_tool_checks_safety_policy_before_executing():
    env = FakeEnv(stdout="ok\n")
    safety = SpySafetyPolicy()

    result = run(BashTool().execute({"command": "echo ok", "timeout": 7}, env=env, interceptor=safety))

    assert safety.cmd_calls == [("echo ok", None)]
    assert env.exec_calls == [("echo ok", 7)]
    assert "stdout:\nok" in result


def test_bash_tool_execute_with_runtime_passes_permission_confirm_fn():
    env = FakeEnv(stdout="ok\n")
    safety = SpySafetyPolicy()
    permission_policy = FakePermissionPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=permission_policy)

    result = run(BashTool().execute_with_runtime({"command": "echo ok"}, runtime))

    assert safety.cmd_calls == [("echo ok", permission_policy.confirm)]
    assert env.exec_calls == [("echo ok", 120.0)]
    assert "stdout:\nok" in result


def test_bash_tool_preserves_sandbox_blocked_command_behavior(tmp_path):
    env = FakeEnv()
    sandbox = SandboxInterceptor(str(tmp_path))

    with pytest.raises(PermissionError):
        run(BashTool().execute({"command": "rm -rf /"}, env=env, interceptor=sandbox))

    assert env.exec_calls == []


def test_bash_tool_execute_with_runtime_preserves_blocked_command_behavior(tmp_path):
    env = FakeEnv()
    sandbox = SandboxInterceptor(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(BashTool().execute_with_runtime({"command": "rm -rf /"}, runtime))

    assert env.exec_calls == []


def test_file_read_uses_safety_policy_path_before_read(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))

    result = run(FileReadTool().execute({"path": "note.txt"}, env=env, interceptor=sandbox))

    assert "File: note.txt (2 lines total, showing 1-2)" in result
    assert "1\talpha" in result


def test_file_read_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))

    with pytest.raises(PermissionError):
        run(FileReadTool().execute({"path": "/etc/passwd"}, env=env, interceptor=sandbox))


def test_file_write_uses_safety_policy_for_create_and_str_replace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))

    create_result = run(
        FileWriteTool().execute(
            {"path": "note.txt", "mode": "create", "content": "alpha\n"},
            env=env,
            interceptor=sandbox,
        )
    )
    replace_result = run(
        FileWriteTool().execute(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            env=env,
            interceptor=sandbox,
        )
    )

    assert "Created/wrote" in create_result
    assert "Replaced in" in replace_result
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "beta\n"


def test_file_write_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))

    with pytest.raises(PermissionError):
        run(
            FileWriteTool().execute(
                {"path": "/tmp/outside.txt", "mode": "create", "content": "nope"},
                env=env,
                interceptor=sandbox,
            )
        )


def test_grep_tool_preserves_env_exec_path_without_path_safety():
    env = FakeEnv(stdout="src/app.py:1:needle\n")
    safety = SpySafetyPolicy()

    result = run(
        GrepTool().execute(
            {"pattern": "needle", "path": "../outside", "glob": "*.py", "max_results": 3},
            env=env,
            interceptor=safety,
        )
    )

    assert result == "src/app.py:1:needle"
    assert safety.path_calls == []
    assert len(env.exec_calls) == 1
    cmd, timeout = env.exec_calls[0]
    assert "needle" in cmd
    assert "../outside" in cmd
    assert timeout == 30


def test_ask_user_tool_preserves_non_interactive_fallback():
    result = run(AskUserTool().execute({"question": "Proceed?"}, confirm_fn=None))

    assert result == "Running in non-interactive mode. Make your own best judgment and proceed."


def test_tool_runtime_confirm_fn_exposes_permission_confirm():
    permission_policy = FakePermissionPolicy()
    with_permission = ToolRuntime(environment=None, safety_policy=None, permission_policy=permission_policy)
    without_permission = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    assert with_permission.confirm_fn() == permission_policy.confirm
    assert without_permission.confirm_fn() is None


def test_base_tool_execute_with_runtime_preserves_legacy_tool_behavior():
    env = FakeEnv(stdout="ok\n")
    safety = SpySafetyPolicy()
    permission_policy = FakePermissionPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=permission_policy)

    result = run(LegacyEchoTool().execute_with_runtime({"command": "echo ok"}, runtime))

    assert safety.cmd_calls == [("echo ok", permission_policy.confirm)]
    assert env.exec_calls == [("echo ok", 120.0)]
    assert result == "ok\n"


def test_bash_tool_does_not_import_inner_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/tools/bash.py").read_text(encoding="utf-8")

    assert "opencollab.core.session" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tools.safety" not in source


def test_tool_modules_do_not_type_against_concrete_sandbox():
    package_root = Path(__file__).resolve().parents[1]
    tool_files = [
        package_root / "opencollab/tools/base.py",
        package_root / "opencollab/tools/bash.py",
        package_root / "opencollab/tools/fs.py",
        package_root / "opencollab/tools/human.py",
        package_root / "opencollab/tools/mcp.py",
    ]

    for path in tool_files:
        source = path.read_text(encoding="utf-8")
        assert "opencollab.tools.safety" not in source
        assert "SandboxInterceptor" not in source
