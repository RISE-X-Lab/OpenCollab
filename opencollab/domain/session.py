from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from opencollab.domain.pending import PendingEventTable
from opencollab.domain.tool_loops import LoopState

MAX_SCOUT_LEDGER_CARDS = 256


def same_snapshot_value(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if isinstance(left, str) and isinstance(right, str):
        # The JSON encoder writes string subclasses, including enums, as text.
        return str.__eq__(left, right)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            same_snapshot_value(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(same_snapshot_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and left == right == 0:
        return math.copysign(1, left) == math.copysign(1, right)
    return left == right


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MessageTimestamp(Enum):
    UNKNOWN = 0


class SessionPhase(Enum):
    IDLE = "idle"
    PRECHECK = "precheck"
    CALLING_LLM = "calling_llm"
    HANDLING_RESPONSE = "handling_response"
    EXECUTING_TOOLS = "executing_tools"
    AWAITING_EVENTS = "awaiting_events"
    AUTOSAVING = "autosaving"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"

    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES = frozenset(
    {
        SessionPhase.DONE,
        SessionPhase.STOPPED,
        SessionPhase.ERROR,
    }
)


# Validated run-loop edges. set_phase is reserved for scheduler birth/enqueue,
# restore, and tests; fail/cancel may escape from any phase to ERROR/STOPPED.
# STOPPED retains the reason and incomplete outcome for parent recovery.
# AWAITING_EVENTS suspends the driver until the whole pending batch is filled,
# then resumes through AUTOSAVING before the next PRECHECK.
PHASE_TRANSITIONS: dict[SessionPhase, frozenset[SessionPhase]] = {
    SessionPhase.IDLE: frozenset({SessionPhase.PRECHECK}),
    SessionPhase.PRECHECK: frozenset(
        {SessionPhase.CALLING_LLM, SessionPhase.STOPPED}
    ),
    SessionPhase.CALLING_LLM: frozenset(
        {SessionPhase.HANDLING_RESPONSE, SessionPhase.STOPPED}
    ),
    # AUTOSAVING is the empty-stop retry route: the turn produced nothing to
    # execute, so it autosaves like any step and loops back to PRECHECK.
    SessionPhase.HANDLING_RESPONSE: frozenset(
        {
            SessionPhase.EXECUTING_TOOLS,
            SessionPhase.DONE,
            SessionPhase.STOPPED,
            SessionPhase.AUTOSAVING,
        }
    ),
    SessionPhase.EXECUTING_TOOLS: frozenset(
        {SessionPhase.AUTOSAVING, SessionPhase.AWAITING_EVENTS}
    ),
    SessionPhase.AWAITING_EVENTS: frozenset({SessionPhase.AUTOSAVING}),
    # PRECHECK on every ordinary step; DONE when the step that just
    # finished included a ``submit`` call. The step is saved either way --
    # the edge is taken after ``finish_step``, so a submitted turn is
    # persisted exactly like any other before it ends.
    SessionPhase.AUTOSAVING: frozenset({SessionPhase.PRECHECK, SessionPhase.DONE}),
    # Terminal phases resume back to IDLE for a fresh user turn or a re-run.
    SessionPhase.DONE: frozenset({SessionPhase.IDLE}),
    SessionPhase.STOPPED: frozenset({SessionPhase.IDLE}),
    SessionPhase.ERROR: frozenset({SessionPhase.IDLE}),
}


class InvalidPhaseTransition(Exception):
    """Raised when the run loop attempts an edge absent from PHASE_TRANSITIONS."""

    def __init__(self, src: SessionPhase, dst: SessionPhase):
        self.src = src
        self.dst = dst
        super().__init__(f"Illegal session phase transition: {src.value} -> {dst.value}")


@dataclass
class TurnEnforcementState:
    """Per-turn signals checkpointed, restored, and reset together.

    Signals are maintained even when enforcement is disabled; only their consuming
    brakes are gated. Durable wind-down latches stay on SessionState and survive
    persistence within a turn.
    """

    # Loop-detection window: path-normalized (tool, args) hashes of recent tool
    # calls; drives the repeated-call loop block.
    recent_call_hashes: list[str] = field(default_factory=list)
    loop_state: LoopState = field(default_factory=LoopState)
    # Unknown write outcomes avoid steering that assumes a known unchanged workspace.
    write_effect_unknown: bool = False
    last_progress_unknown: bool = False
    # file_read/grep calls since a successful edit; drives write escalation.
    reads_since_last_edit: int = 0
    # A landed edit switches the hard gate from writing to structured submission.
    has_landed_write: bool = False
    # Consecutive low-yield results; see record_evidence_signal for novelty rules.
    low_yield_since_progress: int = 0
    # Informative results tallied in the current turn.
    distinct_evidence_count: int = 0
    # Novelty memory for the sensor: holds both content and (tool, args) call
    # hashes, so a re-read at a shifted range still scores zero gain.
    seen_result_hashes: set[str] = field(default_factory=set)
    # Compact result cards retained for final synthesis after interrupted exploration.
    scout_ledger: list[dict[str, Any]] = field(default_factory=list)
    # Progress watchdog (STEP 3): consecutive no-progress tool STEPS (batches),
    # where progress is a landed write OR a novel informative ("hit") result.
    steps_since_progress: int = 0
    # Hard loop-block brake: repeated identical/normalized calls short-circuited;
    # stops the session after repeated warnings.
    loop_blocked_since_progress: int = 0


@dataclass(frozen=True)
class _UserTurnCheckpoint:
    message_count: int
    timestamp_count: int
    phase: SessionPhase
    terminal_reason: str | None
    pending_external_user_turn: dict[str, Any] | None
    pending_step_latency: float | None
    submitted_summary: str | None
    # The per-turn enforcement window, snapshotted and rolled back as one unit.
    turn: TurnEnforcementState
    wind_down_done: bool
    wind_down_attempts: int
    wind_down_token_mark: int


@dataclass
class SessionState:
    messages: list[dict[str, Any]]
    # Native Anthropic requests retain their compacted view beside the complete
    # transcript. Later thinking is bound to this view, including after restore.
    anthropic_history: dict[str, Any] | None = field(default=None, kw_only=True)
    used_tokens: int = 0
    # Real size (provider ``input_tokens``) of the current context as of the
    # last LLM call. 0 means "no real measurement yet — fall back to estimate".
    context_tokens: int = 0
    # Lifetime count: a new user turn must not renew the max_steps allowance.
    step_count: int = 0
    # Lifetime count of calls with recovered tool markup; observability only.
    markup_recovered: int = 0
    # Checkpoint, restore, and reset the turn window as one unit.
    turn: TurnEnforcementState = field(default_factory=TurnEnforcementState)
    # Persist the current turn's protected-submit latch and starting token mark;
    # the latter measures submit_turn_cost. Reset only for a new user turn.
    wind_down_done: bool = False
    # Counts protected provider calls allocated by the wind-down gate. It is
    # durable so a restore cannot re-grant the one compatibility retry.
    wind_down_attempts: int = 0
    wind_down_token_mark: int = 0
    # Session-lifetime latch for the protected call carved from the hard token
    # budget. Per-turn watchdog/low-yield wind-down state resets between user
    # turns, but the same lifetime reserve must never be allocated twice.
    budget_reserve_consumed: bool = field(default=False, kw_only=True)
    phase: SessionPhase = SessionPhase.IDLE
    # Human-readable detail for the current terminal phase (e.g. the exception
    # for ERROR, the token/step counts for the resource caps). ``None`` while
    # the session is non-terminal; cleared on resume to IDLE.
    terminal_reason: str | None = None
    aid: int = -1
    pending_events: PendingEventTable = field(default_factory=PendingEventTable)
    # Per-message creation timestamps (UTC ISO-8601), index-aligned with
    # ``messages``. Kept as a sidecar so ``messages`` stays a clean,
    # API-shaped list; merged into each message only when persisting.
    message_timestamps: list[str | _MessageTimestamp] = field(default_factory=list)
    # User messages queued by the scheduler but not yet appended to
    # ``messages`` because the session is mid-turn or awaiting tool results.
    # These are persisted for observability/recovery without changing the
    # provider-facing history until delivery is safe.
    pending_user_messages: list[dict[str, Any]] = field(default_factory=list)
    # An external user turn has already been appended but no runner has begun
    # it yet. Unlike scheduler-owned ``pending_user_messages``, this records
    # the crash window between accepting a public turn and starting its driver.
    pending_external_user_turn: dict[str, Any] | None = None
    # First message position *after* the history visible when the active turn
    # began. It is retained only while a deferred turn is suspended so a
    # restored runner can keep answer lookup within that turn.
    active_turn_start_message_index: int | None = None
    # None retains whole-history answer lookup for legacy completed snapshots.
    completed_turn_start_message_index: int | None = field(default=None, kw_only=True)
    # Elapsed provider/tool time for a deferred step whose process-local
    # PendingStep response was released while waiting for child results.
    pending_step_latency: float | None = None
    # Accepted completion for a turn still waiting on earlier deferred tools.
    submitted_summary: str | None = None

    def __post_init__(self) -> None:
        self._align_timestamps()

    def _align_timestamps(self) -> None:
        if len(self.message_timestamps) < len(self.messages):
            now = _now_iso()
            self.message_timestamps += [now] * (
                len(self.messages) - len(self.message_timestamps)
            )

    def enriched_messages(self, start: int = 0) -> list[dict[str, Any]]:
        """Messages with their creation ``timestamp`` merged in, for persistence.

        Re-aligns first so messages appended by bypassing ``append_message``
        (e.g. direct list mutation) still get a timestamp.
        """
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("message start must be a non-negative integer")
        self._align_timestamps()
        return [
            dict(msg) if ts is _MessageTimestamp.UNKNOWN else {**msg, "timestamp": ts}
            for msg, ts in zip(
                self.messages[start:],
                self.message_timestamps[start:],
            )
        ]

    def queue_pending_user_message(self, message: dict[str, Any]) -> None:
        self.pending_user_messages.append({**message, "timestamp": _now_iso()})

    def discard_pending_user_message(self, content: str) -> None:
        for index, message in enumerate(self.pending_user_messages):
            if message.get("content") == content:
                del self.pending_user_messages[index]
                return

    def discard_pending_user_message_id(self, message_id: str) -> None:
        for index, message in enumerate(self.pending_user_messages):
            if message.get("message_id") == message_id:
                del self.pending_user_messages[index]
                return

    def enriched_pending_user_messages(self) -> list[dict[str, Any]]:
        return [dict(message) for message in self.pending_user_messages]

    def append_queued_external_user_turn(self, content: str, *, defer: bool = False) -> None:
        """Atomically mark and append a public user turn awaiting its driver."""
        pending = {
            "turn_id": uuid.uuid4().hex,
            "status": "queued",
            "content": content,
        }
        self.pending_external_user_turn = pending
        if defer:
            return
        self.append_message({"role": "user", "content": content})
        pending["message_index"] = len(self.messages) - 1

    def consume_queued_external_user_turn(self) -> None:
        """Mark a queued external turn as claimed by the current runner."""
        if self.pending_external_user_turn is not None:
            if "message_index" not in self.pending_external_user_turn:
                self.append_message({"role": "user", "content": self.pending_external_user_turn["content"]})
            self.pending_external_user_turn = None

    def start_active_turn(self, message_index: int) -> None:
        """Persist the answer-lookup boundary for an active user turn."""
        self.active_turn_start_message_index = message_index
        self.completed_turn_start_message_index = None

    def clear_active_turn(self) -> None:
        """Clear suspended work and retain a completed turn's answer boundary."""
        if self.phase is SessionPhase.DONE and self.active_turn_start_message_index is not None:
            self.completed_turn_start_message_index = self.active_turn_start_message_index
        self.active_turn_start_message_index = None
        self.pending_step_latency = None
        self.submitted_summary = None

    @property
    def is_done(self) -> bool:
        return self.phase == SessionPhase.DONE

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.message_timestamps.append(_now_iso())

    def replace_messages(self, messages: list[dict[str, Any]], *, restoring: bool = False) -> None:
        prior = {id(m): ts for m, ts in zip(self.messages, self.message_timestamps)}
        rebuilt_messages: list[dict[str, Any]] = []
        timestamps: list[str | _MessageTimestamp] = []
        for message in messages:
            if isinstance(message, dict) and "timestamp" in message:
                timestamp = message["timestamp"]
                message = {key: value for key, value in message.items() if key != "timestamp"}
            elif id(message) in prior:
                timestamp = prior[id(message)]
            else:
                # A legacy transcript leaves the message creation time unknown.
                timestamp = _MessageTimestamp.UNKNOWN if restoring else _now_iso()
            rebuilt_messages.append(message)
            timestamps.append(timestamp)
        self.messages = rebuilt_messages
        self.message_timestamps = timestamps
        self.anthropic_history = None

    def add_used_tokens(self, tokens: int) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("used token increment must be a non-negative integer")
        self.used_tokens += tokens

    def add_markup_recovered(self, count: int) -> None:
        self.markup_recovered += count

    def set_used_tokens(self, tokens: int) -> None:
        self.used_tokens = tokens

    def set_context_tokens(self, tokens: int) -> None:
        self.context_tokens = tokens

    def advance_step(self) -> int:
        self.step_count += 1
        return self.step_count

    def set_step_count(self, step_count: int) -> None:
        self.step_count = step_count

    def mark_done(self) -> None:
        self.phase = SessionPhase.DONE
        self.terminal_reason = "completed"

    def clear_done(self) -> None:
        self.resume_to_idle()

    def set_phase(self, phase: SessionPhase) -> None:
        """Unchecked phase assignment reserved for out-of-band sets: scheduler
        process birth/enqueue, snapshot/restore, and tests. Run-loop edges go
        through ``transition_to``; the abnormal terminal escapes go through
        ``fail``/``cancel``. Prefer those over poking ``set_phase`` directly.
        """
        self.phase = phase

    def transition_to(self, phase: SessionPhase, *, reason: str | None = None) -> None:
        """Validated run-loop transition. Raises ``InvalidPhaseTransition`` if
        the edge is absent from ``PHASE_TRANSITIONS``. ``reason`` records the
        ``terminal_reason`` detail when crossing into a terminal phase.
        """
        if phase not in PHASE_TRANSITIONS.get(self.phase, frozenset()):
            raise InvalidPhaseTransition(self.phase, phase)
        self.phase = phase
        if phase.is_terminal() and reason is not None:
            self.terminal_reason = reason

    def fail(self, reason: str | None = None) -> None:
        """Abnormal escape to ERROR from any phase. ERROR has no inbound edge in
        ``PHASE_TRANSITIONS`` by design — it is reachable only through this
        escape (and snapshot/restore). Callers pair it with a raised exception;
        ``reason`` typically carries that exception's description.
        """
        self.phase = SessionPhase.ERROR
        self.terminal_reason = reason or "error"

    def cancel(self, reason: str | None = None) -> None:
        """Out-of-band escape to STOPPED from any phase, used by the scheduler
        when an agent task is killed mid-loop. The in-loop cancel uses the
        validated PRECHECK -> STOPPED edge via ``transition_to`` instead.
        """
        self.phase = SessionPhase.STOPPED
        self.terminal_reason = reason or "cancelled"

    def resume_to_idle(self) -> None:
        """The single validated reset from a terminal phase back to IDLE for a
        fresh turn or re-run. No-op when the phase is not terminal. Clears the
        terminal_reason so it never leaks into the next turn.
        """
        if self.phase.is_terminal():
            self.transition_to(SessionPhase.IDLE)
            self.terminal_reason = None

    def checkpoint_user_turn(self) -> _UserTurnCheckpoint:
        """Capture only state changed while accepting a new user message."""
        return _UserTurnCheckpoint(
            message_count=len(self.messages),
            timestamp_count=len(self.message_timestamps),
            phase=self.phase,
            terminal_reason=self.terminal_reason,
            pending_external_user_turn=copy.deepcopy(self.pending_external_user_turn),
            pending_step_latency=self.pending_step_latency,
            submitted_summary=self.submitted_summary,
            # Deep-copy the whole per-turn window as one pristine, reusable unit.
            turn=copy.deepcopy(self.turn),
            wind_down_done=self.wind_down_done,
            wind_down_attempts=self.wind_down_attempts,
            wind_down_token_mark=self.wind_down_token_mark,
        )

    def restore_user_turn(self, checkpoint: _UserTurnCheckpoint) -> None:
        """Roll back an interrupted user-message append to its commit boundary."""
        del self.messages[checkpoint.message_count :]
        del self.message_timestamps[checkpoint.timestamp_count :]
        self.phase = checkpoint.phase
        self.terminal_reason = checkpoint.terminal_reason
        self.pending_external_user_turn = copy.deepcopy(
            checkpoint.pending_external_user_turn
        )
        self.pending_step_latency = checkpoint.pending_step_latency
        self.submitted_summary = checkpoint.submitted_summary
        # A fresh copy each restore, so the checkpoint stays reusable.
        self.turn = copy.deepcopy(checkpoint.turn)
        self.wind_down_done = checkpoint.wind_down_done
        self.wind_down_attempts = checkpoint.wind_down_attempts
        self.wind_down_token_mark = checkpoint.wind_down_token_mark

    def reset_for_user_turn(self) -> None:
        """Reset turn-local phase, enforcement signals, and wind-down latches.

        Preserve used_tokens, step_count, and the lifetime budget-reserve latch:
        a new user turn must not renew the session's resource allowance.
        """
        self.resume_to_idle()
        self.turn = TurnEnforcementState()
        self.wind_down_done = False
        self.wind_down_attempts = 0
        self.wind_down_token_mark = 0
        self.pending_step_latency = None
        self.submitted_summary = None

    def record_evidence_signal(
        self,
        content_hash: str,
        call_hash: str,
        intrinsic_low_yield: bool,
        *,
        card: dict[str, Any] | None = None,
    ) -> None:
        """Update novelty counters and, when supplied, append an evidence card.

        A result is informative only if it is not intrinsically low-yield and both
        content and normalized-call hashes are unseen. Informative results reset the
        low-yield streak; all others extend it. Both hashes are remembered.
        Cards use the same decision for hit, NO-MATCH, or duplicate; omitting a card
        updates counters only. Brakes consuming these observations are configured elsewhere.
        """
        already_seen = (
            content_hash in self.turn.seen_result_hashes
            or call_hash in self.turn.seen_result_hashes
        )
        self.turn.seen_result_hashes.add(content_hash)
        self.turn.seen_result_hashes.add(call_hash)
        if not intrinsic_low_yield and not already_seen:
            self.turn.low_yield_since_progress = 0
            self.turn.distinct_evidence_count += 1
            outcome = "hit"
        else:
            # Retain the distinction between no result and a duplicate result.
            self.turn.low_yield_since_progress += 1
            outcome = "NO-MATCH" if intrinsic_low_yield else "duplicate"
        if card is not None:
            self.turn.scout_ledger.append(
                {
                    "tool": card.get("tool", ""),
                    "target": card.get("target", ""),
                    "outcome": outcome,
                    "snippet": card.get("snippet", ""),
                }
            )
            if len(self.turn.scout_ledger) > MAX_SCOUT_LEDGER_CARDS:
                del self.turn.scout_ledger[:-MAX_SCOUT_LEDGER_CARDS]

    def remember_tool_call_hash(self, call_hash: str, max_window: int | None = None) -> None:
        self.turn.recent_call_hashes.append(call_hash)
        if max_window is not None and len(self.turn.recent_call_hashes) > max_window:
            expired = self.turn.recent_call_hashes[:-max_window]
            self.turn.recent_call_hashes = self.turn.recent_call_hashes[-max_window:]
            for key in expired:
                if key in self.turn.loop_state.operations and key not in self.turn.recent_call_hashes:
                    operation = self.turn.loop_state.operations.get(key)
                    if operation is not None and operation.inflight_tool_call_id is None:
                        self.turn.loop_state.operations.pop(key, None)

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.turn.recent_call_hashes = call_hashes
