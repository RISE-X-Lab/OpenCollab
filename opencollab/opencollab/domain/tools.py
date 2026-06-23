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


@dataclass(frozen=True)
class LoopDetection:
    tool: str
    count: int


@dataclass
class ToolProcessingResult:
    messages_to_append: list[dict[str, Any]] = field(default_factory=list)
    recent_hash_updates: list[str] = field(default_factory=list)
    loop_detections: list[LoopDetection] = field(default_factory=list)
    # Closed-loop steering counters for this batch: how many read-only tool calls
    # executed, and whether a write landed. A successful write RESETS
    # ``reads_since_last_edit``; otherwise the reads accumulate onto it.
    reads_executed: int = 0
    write_succeeded: bool = False

    def apply_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
        for message in self.messages_to_append:
            state.append_message(message)
        self.apply_hashes_to(state, max_window=max_window)
        self.apply_read_write_counter_to(state)

    def apply_read_write_counter_to(self, state: SessionState) -> None:
        """Fold this batch's read/write activity into ``reads_since_last_edit``.

        A landed edit zeroes the counter (the model committed); a batch with no
        edit adds its read calls so the steering layer can escalate when the
        model keeps reading without writing.
        """
        if self.write_succeeded:
            state.reads_since_last_edit = 0
        else:
            state.reads_since_last_edit += self.reads_executed

    def apply_hashes_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
        """Apply only the loop-detection hashes, not the result messages.

        Used when a batch contains a deferred tool: immediate results are
        buffered into the pending table (to keep the whole batch's tool-result
        block contiguous on resume) while their call hashes still record now.
        """
        for call_hash in self.recent_hash_updates:
            state.remember_tool_call_hash(call_hash, max_window=max_window)


__all__ = [
    "LoopDetection",
    "MAX_CALL_HASH_WINDOW",
    "ToolProcessingResult",
    "ToolSpec",
]
