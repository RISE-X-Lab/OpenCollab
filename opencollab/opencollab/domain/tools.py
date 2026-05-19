from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from opencollab.domain.session import SessionState

MAX_CALL_HASH_WINDOW = 200


class ToolSpec(Protocol):
    """The schema surface every tool exposes to an agent.

    Domain-side Protocol describing only what an Agent needs at configuration
    time: name + description + parameters + OpenAI schema rendering. Tool
    execution lives in application.ports.ToolPort.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        ...


@dataclass
class ToolProcessingResult:
    messages_to_append: list[dict[str, Any]] = field(default_factory=list)
    recent_hash_updates: list[str] = field(default_factory=list)
    loop_detections: list[dict[str, Any]] = field(default_factory=list)

    def apply_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
        for message in self.messages_to_append:
            state.append_message(message)
        for call_hash in self.recent_hash_updates:
            state.remember_tool_call_hash(call_hash, max_window=max_window)


__all__ = [
    "MAX_CALL_HASH_WINDOW",
    "ToolProcessingResult",
    "ToolSpec",
]
