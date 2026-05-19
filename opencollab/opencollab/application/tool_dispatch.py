from __future__ import annotations

from typing import Any

from opencollab.application.tool_runtime import ToolRuntime


async def execute_tool_with_runtime(
    tool: Any,
    params: dict[str, Any],
    runtime: ToolRuntime,
) -> str:
    return await tool.execute_with_runtime(params, runtime)
