from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionPhase(Enum):
    IDLE = "idle"
    PRECHECK = "precheck"
    COMPACTING = "compacting"
    CALLING_LLM = "calling_llm"
    HANDLING_RESPONSE = "handling_response"
    EXECUTING_TOOLS = "executing_tools"
    AUTOSAVING = "autosaving"
    DONE = "done"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"


@dataclass
class SessionState:
    messages: list[dict[str, Any]]
    used_tokens: int = 0
    step_count: int = 0
    is_done: bool = False
    recent_call_hashes: list[str] = field(default_factory=list)
    phase: SessionPhase = SessionPhase.IDLE

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def add_used_tokens(self, tokens: int) -> None:
        self.used_tokens += tokens

    def set_used_tokens(self, tokens: int) -> None:
        self.used_tokens = tokens

    def advance_step(self) -> int:
        self.step_count += 1
        return self.step_count

    def set_step_count(self, step_count: int) -> None:
        self.step_count = step_count

    def mark_done(self, done: bool = True) -> None:
        self.is_done = done

    def set_phase(self, phase: SessionPhase) -> None:
        self.phase = phase

    def reset_for_user_turn(self) -> None:
        self.mark_done(False)
        self.clear_recent_tool_hashes()

    def remember_tool_call_hash(self, call_hash: str, max_window: int | None = None) -> None:
        self.recent_call_hashes.append(call_hash)
        if max_window is not None and len(self.recent_call_hashes) > max_window:
            self.recent_call_hashes = self.recent_call_hashes[-max_window:]

    def clear_recent_tool_hashes(self) -> None:
        self.recent_call_hashes.clear()

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.recent_call_hashes = call_hashes
