"""Stable tool exports and curated coding-tool construction."""

from __future__ import annotations

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool, verification_run_tests_tool


def coding_toolset(
    *,
    require_process_isolation: bool = False,
    allow_test_command_overrides: bool = True,
    allow_file_creation: bool = True,
) -> tuple[Tool, ...]:
    """Build a fresh, ordered tool set for a coding workflow.

    Evaluation callers should require process isolation and disable test
    command overrides. Each invocation creates new stateful tool instances.
    """
    return (
        BashTool(require_process_isolation=require_process_isolation),
        FileReadTool(),
        FileWriteTool(allow_create=allow_file_creation),
        ApplyPatchTool(),
        RunTestsTool(
            allow_runner_override=allow_test_command_overrides,
            allow_extra_args=allow_test_command_overrides,
            require_process_isolation=require_process_isolation,
        ),
        GitDiffTool(),
        GrepTool(),
    )


__all__ = [
    "ApplyPatchTool",
    "BashTool",
    "FileReadTool",
    "FileWriteTool",
    "GitDiffTool",
    "GrepTool",
    "RunTestsTool",
    "Tool",
    "coding_toolset",
    "verification_run_tests_tool",
]
