from __future__ import annotations

import asyncio
import json
import os

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.base import host_write_lock
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.environments import local_environment
from opencollab.tools import builtin_tools


class _PathModel:
    def __init__(self, tool, arguments):
        self.tool = tool
        self.arguments = arguments
        self.called = False

    async def complete(self, messages, **kwargs):
        if self.called:
            return LLMResponse(content="finished", usage=Usage(10, 5), finish_reason="stop")
        self.called = True
        return LLMResponse(tool_calls=[{
            "id": "selected-file", "type": "function", "function": {
                "name": self.tool, "arguments": json.dumps(self.arguments),
            },
        }], usage=Usage(10, 5), finish_reason="tool_calls")


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("tool", ["file_read", "file_write", "apply_patch"])
async def test_sdk_file_tools_match_shell_through_symlink_parent(tmp_path, absolute, tool):
    (tmp_path / "actual/child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("actual/child", target_is_directory=True)
    target = tmp_path / "actual/module.py"
    unrelated = tmp_path / "module.py"
    target.write_text("VALUE = 1\n")
    unrelated.write_text("UNRELATED = 7\n" if tool == "file_read" else "VALUE = 1\n")
    raw = "alias/../module.py"
    path = os.path.join(str(tmp_path), raw) if absolute else raw
    arguments = {"path": path}
    if tool == "file_write":
        arguments.update(mode="str_replace", old_str="VALUE = 1", new_str="VALUE = 42")
    elif tool == "apply_patch":
        arguments.update(
            mode="line_replace", start_line=1, end_line=1,
            expected_str="VALUE = 1", new_str="VALUE = 42",
        )
    model = _PathModel(tool, arguments)
    environment = local_environment(tmp_path)
    outputs = []

    async def flow(ctx, _):
        result = await ctx.agent_run(
            "Use the requested file tool", tools=list(builtin_tools(tool, headless=False)),
            isolation="shared", budget=2000, system_prompt="Use the requested file tool",
        )
        outputs.extend(
            message["content"] for session in ctx.sessions for message in session.state.messages
            if message.get("role") == "tool"
        )
        return result.status

    try:
        result = await OpenCollab(tmp_path, environment=environment, model="local-scripted").workflow(
            flow, llm=model, trace=False, timeout=15, budget=5000, concurrency=1,
            limit_mode="explicit", agent_profile="single2",
        )
        assert result.ok, result.reason
        assert result.output == "completed"
        assert len(outputs) == 1 and not outputs[0].startswith("Error")
        if tool == "file_read":
            assert "1\tVALUE = 1" in outputs[0]
            assert "UNRELATED" not in outputs[0]
        else:
            assert target.read_text() == "VALUE = 42\n"
        assert unrelated.read_text() == ("UNRELATED = 7\n" if tool == "file_read" else "VALUE = 1\n")
        shell = await environment.exec_cmd("cat alias/../module.py")
        assert shell.stdout == target.read_text()
    finally:
        await environment.cleanup()


async def test_native_edit_uses_the_selected_physical_file_lock(tmp_path):
    (tmp_path / "actual/child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("actual/child", target_is_directory=True)
    target = tmp_path / "actual/module.py"
    target.write_text("VALUE = 1\n")
    unrelated = tmp_path / "module.py"
    unrelated.write_text("VALUE = 1\n")
    environment = local_environment(tmp_path)
    runtime = ToolRuntime(environment, SandboxInterceptor(str(tmp_path)), None)
    editor = None
    try:
        async with host_write_lock(str(target), environment):
            editor = asyncio.create_task(FileWriteTool().execute_with_runtime({
                "path": "alias/../module.py", "mode": "str_replace",
                "old_str": "VALUE = 1", "new_str": "VALUE = 42",
            }, runtime))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(editor), 0.05)
            assert target.read_text() == "VALUE = 1\n"
            assert unrelated.read_text() == "VALUE = 1\n"
        assert not (await asyncio.wait_for(editor, 2)).startswith("Error")
        assert target.read_text() == "VALUE = 42\n"
        assert unrelated.read_text() == "VALUE = 1\n"
    finally:
        if editor is not None and not editor.done():
            editor.cancel()
            await asyncio.gather(editor, return_exceptions=True)
        await environment.cleanup()


@pytest.mark.parametrize("component", ["missing", "regular-file"])
def test_workspace_policy_checks_directory_before_parent_traversal(tmp_path, component):
    target = tmp_path / "value.txt"
    target.write_text("original\n")
    if component == "regular-file":
        (tmp_path / "component").write_text("regular\n")
    policy = SandboxInterceptor(str(tmp_path))
    with pytest.raises(OSError):
        policy.check_path("component/../value.txt")
    assert target.read_text() == "original\n"
    (tmp_path / "sub").mkdir()
    assert policy.check_path("sub/../new/value.txt") == str(tmp_path / "new/value.txt")


def test_workspace_policy_root_resolves_symlink_before_parent(tmp_path):
    (tmp_path / "actual/child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("actual/child", target_is_directory=True)
    policy = SandboxInterceptor(os.path.join(str(tmp_path), "alias/.."))
    assert policy.check_path("module.py") == str(tmp_path / "actual/module.py")
