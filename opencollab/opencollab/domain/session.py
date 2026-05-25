from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from opencollab.domain.pending import PendingEventTable


class SessionPhase(Enum):
    SCHEDULED = "scheduled"
    IDLE = "idle"
    PRECHECK = "precheck"
    COMPACTING = "compacting"
    CALLING_LLM = "calling_llm"
    HANDLING_RESPONSE = "handling_response"
    EXECUTING_TOOLS = "executing_tools"
    AWAITING_EVENTS = "awaiting_events"
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
# transitions the loop may make. ``transition_to`` validates against this.
# ``set_phase`` is the unchecked primitive reserved for process birth/enqueue
# by the Scheduler, snapshot/restore, and tests. Two named escapes may fire
# from any phase: ``fail`` -> ERROR and ``cancel`` -> CANCELLED. ERROR is only
# ever reached that way (it has no inbound edge below); CANCELLED also has a
# validated in-loop edge from PRECHECK, so the scheduler's out-of-band
# ``cancel`` covers the case where an agent task is killed mid-loop.
#
# AWAITING_EVENTS is a non-terminal *suspend* state: the loop stops there (the
# task returns) when a step deferred work (e.g. a spawned child) and the
# scheduler re-activates the session, resuming at PRECHECK, once every pending
# row is filled. EXECUTING_TOOLS branches to AUTOSAVING (all tools immediate) or
# AWAITING_EVENTS (any deferred tool present).
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
    SessionPhase.EXECUTING_TOOLS: frozenset(
        {SessionPhase.AUTOSAVING, SessionPhase.AWAITING_EVENTS}
    ),
    SessionPhase.AWAITING_EVENTS: frozenset({SessionPhase.PRECHECK}),
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
    pending_events: PendingEventTable = field(default_factory=PendingEventTable)

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
        self.resume_to_idle()

    def set_phase(self, phase: SessionPhase) -> None:
        """Unchecked phase assignment reserved for out-of-band sets: scheduler
        process birth/enqueue, snapshot/restore, and tests. Run-loop edges go
        through ``transition_to``; the abnormal terminal escapes go through
        ``fail``/``cancel``. Prefer those over poking ``set_phase`` directly.
        """
        self.phase = phase

    def transition_to(self, phase: SessionPhase) -> None:
        """Validated run-loop transition. Raises ``InvalidPhaseTransition`` if
        the edge is absent from ``PHASE_TRANSITIONS``.
        """
        if phase not in PHASE_TRANSITIONS.get(self.phase, frozenset()):
            raise InvalidPhaseTransition(self.phase, phase)
        self.phase = phase

    def fail(self) -> None:
        """Abnormal escape to ERROR from any phase. ERROR has no inbound edge in
        ``PHASE_TRANSITIONS`` by design — it is reachable only through this
        escape (and snapshot/restore). Callers pair it with a raised exception.
        """
        self.phase = SessionPhase.ERROR

    def cancel(self) -> None:
        """Out-of-band escape to CANCELLED from any phase, used by the scheduler
        when an agent task is killed mid-loop. The in-loop cancel uses the
        validated PRECHECK -> CANCELLED edge via ``transition_to`` instead.
        """
        self.phase = SessionPhase.CANCELLED

    def resume_to_idle(self) -> None:
        """The single validated reset from a terminal phase back to IDLE for a
        fresh turn or re-run. No-op when the phase is not terminal.
        """
        if self.phase.is_terminal():
            self.transition_to(SessionPhase.IDLE)

    def reset_for_user_turn(self) -> None:
        self.resume_to_idle()
        self.clear_recent_tool_hashes()

    def remember_tool_call_hash(self, call_hash: str, max_window: int | None = None) -> None:
        self.recent_call_hashes.append(call_hash)
        if max_window is not None and len(self.recent_call_hashes) > max_window:
            self.recent_call_hashes = self.recent_call_hashes[-max_window:]

    def clear_recent_tool_hashes(self) -> None:
        self.recent_call_hashes.clear()

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.recent_call_hashes = call_hashes
