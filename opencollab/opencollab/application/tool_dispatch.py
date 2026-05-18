from __future__ import annotations

from typing import Any

from opencollab.application.tool_runtime import ToolRuntime


async def execute_tool_with_runtime(
    tool: Any,
    params: dict[str, Any],
    runtime: ToolRuntime,
) -> str:
    execute_with_runtime = getattr(tool, "execute_with_runtime", None)
    if execute_with_runtime is not None:
        return await execute_with_runtime(params, runtime)
    return await execute_legacy_tool(tool, params, runtime)


async def execute_legacy_tool(
    tool: Any,
    params: dict[str, Any],
    runtime: ToolRuntime,
) -> str:
    return await tool.execute(
        params,
        env=runtime.environment,
        interceptor=runtime.safety_policy,
        confirm_fn=runtime.confirm_fn(),
    )
