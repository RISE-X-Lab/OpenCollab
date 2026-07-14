from __future__ import annotations

import asyncio

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime


def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


def test_allow_create_false_rejects_create_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = FileWriteTool(allow_create=False)

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "new.py", "mode": "create", "content": "x = 1\n"},
            _runtime(ws),
        )
    )

    assert result.startswith("Error:")
    assert "file creation disabled" in result
    assert not (ws / "new.py").exists()


def test_allow_create_false_still_allows_str_replace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")
    tool = FileWriteTool(allow_create=False)

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "f.py", "mode": "str_replace", "old_str": "world", "new_str": "there"},
            _runtime(ws),
        )
    )

    assert "content changed" in result
    assert target.read_text(encoding="utf-8") == "hello there\n"


def test_allow_create_true_default_creates_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = FileWriteTool()

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "new.py", "mode": "create", "content": "x = 1\n"},
            _runtime(ws),
        )
    )

    assert "Created/wrote" in result
    assert (ws / "new.py").read_text(encoding="utf-8") == "x = 1\n"
