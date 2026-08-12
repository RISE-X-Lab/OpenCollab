"""StructuredOutputTool — capture a schema-validated payload from an agent.

A pure application-layer tool implementing the ``ToolPort`` protocol directly
(no adapter import). The workflow engine injects one of these into a session
when ``WorkflowContext.agent`` is called with ``schema=``; the agent is told to
finish by calling ``structured_output`` with its result. ``execute_with_runtime``
validates the call against the caller's schema:

* valid  -> stores the payload on ``self.captured``, fires ``on_capture``
  (the engine wires this to a cancel event so the session halts before its
  next LLM call), and acknowledges,
* invalid -> returns the validation errors as the tool result string so the
  model self-corrects within its own run loop.

Pure application layer: stdlib + application imports only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opencollab.application.schema_validate import validate, validate_schema
from opencollab.application.tool_execution import ToolRuntime

TOOL_NAME = "structured_output"


class StructuredOutputTool:
    """Captures a schema-validated payload; implements ``ToolPort`` structurally.

    Stateful by design: ``captured`` holds the last valid payload (or ``None``
    if none has been accepted yet). One instance is built per ``agent(schema=)``
    call, so there is no cross-call leakage.
    """

    name = TOOL_NAME
    default_timeout: float | None = None
    disable_outer_timeout = False
    description = (
        "Return your final answer as structured data. Call this exactly once, "
        "at the end, with the result object that conforms to the required schema. "
        "If the arguments are invalid you will be told what is wrong; fix them "
        "and call structured_output again."
    )

    def __init__(
        self,
        schema: dict[str, Any],
        on_capture: Callable[[], None] | None = None,
    ) -> None:
        schema_errors = validate_schema(schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema_errors.append(
                "$schema.type: structured-output tool top-level type must be 'object'"
            )
        if schema_errors:
            raise ValueError(
                "structured output schema is unsupported: " + "; ".join(schema_errors)
            )
        self._schema = schema
        self._on_capture = on_capture
        self.parameters = schema
        self.captured: dict[str, Any] | None = None
        self.terminal_capture_accepted = False

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        self.terminal_capture_accepted = False
        errors = validate(params, self._schema)
        if errors:
            joined = "; ".join(errors)
            return (
                "Validation failed; the output does not conform to the schema. "
                f"Fix and call {self.name} again. Errors: {joined}"
            )
        self.captured = params
        self.terminal_capture_accepted = True
        if self._on_capture is not None:
            self._on_capture()
        return "Recorded. Structured output accepted. Your task is complete."


__all__ = ["StructuredOutputTool", "TOOL_NAME"]
