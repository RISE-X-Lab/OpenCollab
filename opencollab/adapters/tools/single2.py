"""Single2 instances of OpenCollab 0.7's native coding tools."""

from __future__ import annotations

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool

SINGLE2_BASH_OUTPUT_CHARS = 10_000


def build_single2_tools(*, headless: bool = True) -> tuple[Tool, ...]:
    """Return the six source-compatible tools in their evaluated order."""
    return (
        BashTool(
            max_output_chars=SINGLE2_BASH_OUTPUT_CHARS,
            require_process_isolation=headless,
        ),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        GitDiffTool(),
        GrepTool(),
    )


__all__ = ["SINGLE2_BASH_OUTPUT_CHARS", "build_single2_tools"]
