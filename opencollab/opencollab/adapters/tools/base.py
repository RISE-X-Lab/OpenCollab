"""Tool base class — JSON Schema driven, async execution.

First Principle: A Tool is a function with a JSON Schema input and string output.
LLM calls tools via function calling; the framework routes to the right
Tool.execute_with_runtime().

Ref:
- kimi-cli: CallableTool2[Params] with Pydantic schema + async __call__
- opencode: ToolRegistry with init/execute pattern
- openclaw: AnyAgentTool structural interface
"""

from __future__ import annotations

from typing import Any

from opencollab.application.tool_execution import ToolRuntime


class Tool:
    """Base class for all tools. Subclass and implement ``execute_with_runtime``.

    Attributes:
        name: Tool name as seen by the LLM (snake_case recommended).
        description: One-line description for the LLM to understand when to use it.
        parameters: JSON Schema dict describing the input parameters.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        """Execute the tool with the runtime bundle. Returns result as string."""
        raise NotImplementedError(
            f"Tool '{self.name}' must implement execute_with_runtime()"
        )
