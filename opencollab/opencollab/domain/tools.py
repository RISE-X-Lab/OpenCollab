from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opencollab.domain.session import SessionState

MAX_CALL_HASH_WINDOW = 200


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
