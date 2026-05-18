"""Canonical default-tool bundle for agents.

Tools are stateless; session/team composition passes safety policy into the
ToolCallProcessor for each tool.execute() call.
"""

from __future__ import annotations

from opencollab.tools.base import Tool
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.tools.human import AskUserTool


def build_default_tools(*, include_ask_user: bool = False) -> list[Tool]:
    """Canonical tool bundle: bash, file_read, file_write, grep, [ask_user]."""
    tools: list[Tool] = [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        GrepTool(),
    ]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools
