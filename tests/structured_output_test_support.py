"""Shared test doubles for structured workflow output tests."""

from opencollab.application.tool_execution import ToolRuntime


class NamedTool:
    description = "test"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str) -> None:
        self.name = name

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def runtime() -> ToolRuntime:
    return ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=None,
    )
