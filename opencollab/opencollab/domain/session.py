from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionPhase(Enum):
    SCHEDULED = "scheduled"
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

    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES = frozenset(
    {
        SessionPhase.DONE,
        SessionPhase.CANCELLED,
        SessionPhase.BUDGET_EXCEEDED,
        SessionPhase.ERROR,
    }
)


# The run-loop FSM topology — the single source of truth for which phase
# transitions the loop may make. ``transition_to`` validates against this;
# ``set_phase`` is the unchecked primitive for out-of-band sets (process
# birth/enqueue by the Scheduler, the ERROR escape on exception, snapshot/
# restore, and tests). Entering ERROR is deliberately *not* a normal edge — it
# is an abnormal escape applied via ``set_phase`` from any phase.
PHASE_TRANSITIONS: dict[SessionPhase, frozenset[SessionPhase]] = {
    SessionPhase.SCHEDULED: frozenset({SessionPhase.IDLE}),
    SessionPhase.IDLE: frozenset({SessionPhase.PRECHECK}),
    SessionPhase.PRECHECK: frozenset(
        {
            SessionPhase.COMPACTING,
            SessionPhase.CALLING_LLM,
            SessionPhase.CANCELLED,
            SessionPhase.BUDGET_EXCEEDED,
        }
    ),
    SessionPhase.COMPACTING: frozenset({SessionPhase.CALLING_LLM}),
    SessionPhase.CALLING_LLM: frozenset({SessionPhase.HANDLING_RESPONSE}),
    SessionPhase.HANDLING_RESPONSE: frozenset({SessionPhase.EXECUTING_TOOLS, SessionPhase.DONE}),
    SessionPhase.EXECUTING_TOOLS: frozenset({SessionPhase.AUTOSAVING}),
    SessionPhase.AUTOSAVING: frozenset({SessionPhase.PRECHECK}),
    # Terminal phases resume back to IDLE for a fresh user turn or a re-run.
    SessionPhase.DONE: frozenset({SessionPhase.IDLE}),
    SessionPhase.CANCELLED: frozenset({SessionPhase.IDLE}),
    SessionPhase.BUDGET_EXCEEDED: frozenset({SessionPhase.IDLE}),
    SessionPhase.ERROR: frozenset({SessionPhase.IDLE}),
}


class InvalidPhaseTransition(Exception):
    """Raised when the run loop attempts an edge absent from PHASE_TRANSITIONS."""

    def __init__(self, src: SessionPhase, dst: SessionPhase):
        self.src = src
        self.dst = dst
        super().__init__(f"Illegal session phase transition: {src.value} -> {dst.value}")


@dataclass
class SessionState:
    messages: list[dict[str, Any]]
    used_tokens: int = 0
    step_count: int = 0
    recent_call_hashes: list[str] = field(default_factory=list)
    phase: SessionPhase = SessionPhase.IDLE
    aid: int = -1

    @property
    def is_done(self) -> bool:
        return self.phase == SessionPhase.DONE

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

    def mark_done(self) -> None:
        self.phase = SessionPhase.DONE

    def clear_done(self) -> None:
        if self.phase == SessionPhase.DONE:
            self.phase = SessionPhase.IDLE

    def set_phase(self, phase: SessionPhase) -> None:
        """Unchecked phase assignment for out-of-band sets (scheduler birth/
        enqueue, the ERROR escape, snapshot/restore, tests). Run-loop edges
        should go through ``transition_to`` so illegal transitions fail loudly.
        """
        self.phase = phase

    def transition_to(self, phase: SessionPhase) -> None:
        """Validated run-loop transition. Raises ``InvalidPhaseTransition`` if
        the edge is absent from ``PHASE_TRANSITIONS``.
        """
        if phase not in PHASE_TRANSITIONS.get(self.phase, frozenset()):
            raise InvalidPhaseTransition(self.phase, phase)
        self.phase = phase

    def reset_for_user_turn(self) -> None:
        self.clear_done()
        self.clear_recent_tool_hashes()

    def remember_tool_call_hash(self, call_hash: str, max_window: int | None = None) -> None:
        self.recent_call_hashes.append(call_hash)
        if max_window is not None and len(self.recent_call_hashes) > max_window:
            self.recent_call_hashes = self.recent_call_hashes[-max_window:]

    def clear_recent_tool_hashes(self) -> None:
        self.recent_call_hashes.clear()

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.recent_call_hashes = call_hashes
