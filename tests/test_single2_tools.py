from __future__ import annotations

import pytest

from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.single2_safety import wrap_single2_safety
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.single2 import build_single2_tools


def test_single2_uses_native_tools_in_the_evaluated_order():
    tools = build_single2_tools()

    assert tuple(tool.name for tool in tools) == (
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "git_diff",
        "grep",
    )
    assert tuple(type(tool) for tool in tools) == (
        BashTool,
        FileReadTool,
        FileWriteTool,
        ApplyPatchTool,
        GitDiffTool,
        GrepTool,
    )
    assert tools[0].max_output_chars == 10_000
    assert tools[0].require_process_isolation is True


def test_single2_tools_can_be_explicitly_composed_for_local_interaction():
    tools = build_single2_tools(headless=False)

    assert tools[0].require_process_isolation is False


@pytest.mark.parametrize(
    "command",
    [
        "grep -rl needle /",
        "grep --recursive needle / 2>/dev/null",
        "rg needle /",
        "find / -name '*.py'",
    ],
)
def test_single2_blocks_root_recursive_searches(tmp_path, command):
    policy = wrap_single2_safety(SandboxInterceptor(str(tmp_path)), str(tmp_path))

    with pytest.raises(
        PermissionError,
        match="Blocked container-wide recursive search",
    ):
        policy.check_cmd(command)


@pytest.mark.parametrize(
    "command",
    [
        "grep -rl needle .",
        "rg needle src tests",
        "find . -name '*.py'",
    ],
)
def test_single2_allows_workspace_relative_searches(tmp_path, command):
    policy = wrap_single2_safety(SandboxInterceptor(str(tmp_path)), str(tmp_path))

    policy.check_cmd(command)


def test_single2_safety_preserves_existing_path_and_command_rules(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = wrap_single2_safety(
        SandboxInterceptor(str(workspace)),
        str(workspace),
    )

    assert policy.check_path("src.py") == str(workspace / "src.py")
    with pytest.raises(PermissionError, match="Path escapes workspace"):
        policy.check_path("../outside.py")
    with pytest.raises(PermissionError, match="Blocked destructive command"):
        policy.check_cmd("rm -rf /")


def test_default_safety_keeps_open_collab_07_behavior(tmp_path):
    policy = SandboxInterceptor(str(tmp_path))

    policy.check_cmd("find / -name '*.py'")
