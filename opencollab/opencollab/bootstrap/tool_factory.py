"""Canonical default-tool bundle for agents.

Centralizing the tool list means the future interceptor unification (step 5)
only has to touch this file.
"""

from __future__ import annotations

from opencollab.tools.base import Tool
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.tools.human import AskUserTool
from opencollab.tools.safety import SandboxInterceptor


def build_default_tools(
    interceptor: SandboxInterceptor,
    *,
    include_ask_user: bool = False,
) -> list[Tool]:
    """Canonical tool bundle: bash, file_read, file_write, grep, [ask_user]."""
    tools: list[Tool] = [
        BashTool(interceptor),
        FileReadTool(interceptor),
        FileWriteTool(interceptor),
        GrepTool(interceptor),
    ]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools
