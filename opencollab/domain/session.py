from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from opencollab.domain.pending import PendingEventTable

MAX_SCOUT_LEDGER_CARDS = 256


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# The run-loop FSM topology — the single source of truth for which phase
# transitions the loop may make. ``transition_to`` validates against this.
# ``set_phase`` is the unchecked primitive reserved for process birth/enqueue
# by the Scheduler, snapshot/restore, and tests. Two named escapes may fire
# from any phase: ``fail`` -> ERROR and ``cancel`` -> STOPPED. ERROR is only
# ever reached that way (it has no inbound edge below); STOPPED also has a
# validated in-loop edge from PRECHECK, so the scheduler's out-of-band
# ``cancel`` covers the case where an agent task is killed mid-loop.
#
# Three terminals: DONE (the turn produced a final answer), ERROR (an unhandled
# fault, reached only via ``fail``), and STOPPED — one graceful-stop terminal
# covering every controlled halt. The *why* of a STOPPED lives in
# ``terminal_reason`` (a human-readable string), not in a parallel set of enum
# members: a spent token budget, a reached step limit, a repeated-tool-call loop
# block, a user cancel, and a context-window overflow all resolve to
# STOPPED(reason=...). STOPPED is reached from PRECHECK (budget / step / loop /
# cancel gates) and from CALLING_LLM (context overflow: the provider rejected the
# prompt as too large even after a forced maximal compaction pass + one retry —
# e.g. the pinned identity/team/task seed alone exceeds the window). Every STOPPED
# is a controlled stop that delivers an explicit failed result to the parent,
# allowing it to recover without treating unfinished work as successful.
#
# AWAITING_EVENTS is a non-terminal *suspend* state: the loop stops there (the
# task returns) when a step deferred work (e.g. a spawned child) and the
# scheduler re-activates the session, resuming at PRECHECK, once every pending
# row is filled. EXECUTING_TOOLS branches to AUTOSAVING (all tools immediate) or
# AWAITING_EVENTS (any deferred tool present).
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
    SessionPhase.AWAITING_EVENTS: frozenset({SessionPhase.PRECHECK}),
    SessionPhase.AUTOSAVING: frozenset({SessionPhase.PRECHECK}),
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
    """The per-turn enforcement window — the brake panel's per-turn half.

    These eight fields are the unit that ``checkpoint_user_turn`` /
    ``restore_user_turn`` snapshot and roll back as a whole, and that
    ``reset_for_user_turn`` clears on a fresh user turn. They live in their own
    record — rather than smeared flat on ``SessionState`` — precisely because
    one mechanism (the user-turn boundary) saves, restores, and resets them
    together, the same reason a kernel PCB carves its saved-register set into a
    dedicated ``struct``. Session-*lifetime* latches (``wind_down_done`` /
    ``wind_down_token_mark``) have a different reset discipline and stay flat on
    ``SessionState``; they are not part of this snapshot.

    All eight are maintained ALWAYS (cheap, observational); only the precheck
    brakes that READ them are gated behind ``enforcement_strength``, so a
    self-regulating run is byte-for-byte unchanged.
    """

    # Loop-detection window: path-normalized (tool, args) hashes of recent tool
    # calls; drives the repeated-call loop block.
    recent_call_hashes: list[str] = field(default_factory=list)
    # Closed-loop steering signal: read-only tool calls (file_read/grep) since
    # the last successful edit. Drives the reads-without-write nudge/escalation;
    # reset to 0 on a successful write.
    reads_since_last_edit: int = 0
    # Information-gain sensor (STEP 1): consecutive low-yield results since the
    # last informative one (reset to 0 on any informative result). A result is
    # "informative" when NOVEL (unseen content OR unseen path-normalized call
    # hash) AND not an empty/"No matches"-class result; else "low-yield".
    low_yield_since_progress: int = 0
    # Informative results tallied in the current turn.
    distinct_evidence_count: int = 0
    # Novelty memory for the sensor: holds both content and (tool, args) call
    # hashes, so a re-read at a shifted range still scores zero gain.
    seen_result_hashes: set[str] = field(default_factory=set)
    # Evidence ledger (STEP 2): one compact ``{tool, target, outcome, snippet}``
    # card per executed tool result, read by the harvest backstop / dead-scout
    # synthesizer to salvage a chopped scout.
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
    # The per-turn enforcement window, snapshotted and rolled back as one unit.
    turn: TurnEnforcementState
    wind_down_done: bool
    wind_down_attempts: int
    wind_down_token_mark: int


@dataclass
class SessionState:
    messages: list[dict[str, Any]]
    used_tokens: int = 0
    # Real size (provider ``input_tokens``) of the current context as of the
    # last LLM call. 0 means "no real measurement yet — fall back to estimate".
    context_tokens: int = 0
    # Cumulative across the whole session lifetime, NOT per user turn. Compared
    # against ``max_steps`` in PRECHECK; deliberately not reset by
    # ``reset_for_user_turn`` (see that method), so ``max_steps`` is a
    # lifetime cap mirroring the ``used_tokens`` budget.
    step_count: int = 0
    # Lifetime count of LLM calls whose kimi tool-call markup was recovered from
    # literal text (P6). Observability only; like ``used_tokens`` it is a
    # session-lifetime tally and is NOT reset by ``reset_for_user_turn``.
    markup_recovered: int = 0
    # The per-turn enforcement window (loop hashes, reads/low-yield/watchdog
    # counters, novelty memory, evidence ledger) — see ``TurnEnforcementState``.
    # It is snapshotted/rolled-back by checkpoint/restore and cleared by
    # ``reset_for_user_turn`` as one unit, which is why it lives in its own record
    # rather than smeared flat here.
    turn: TurnEnforcementState = field(default_factory=TurnEnforcementState)
    # === enforcement: durable current-turn latches ===
    # ``wind_down_done`` flips True the first time the enforcement precheck forces
    # a scout into its single protected submit turn — the latch that grants
    # EXACTLY ONE more CALLING_LLM and then routes to a terminal.
    # ``wind_down_token_mark`` records ``used_tokens`` at the trip so the
    # commitment-terminus metric can report ``submit_turn_cost``. Both are inert
    # unless ``enforcement_strength`` is on, so a self-regulating run never touches
    # them. They survive persistence within a turn but reset for a new user turn.
    wind_down_done: bool = False
    # Counts protected provider calls allocated by the wind-down gate. It is
    # durable so a restore cannot re-grant the one compatibility retry.
    wind_down_attempts: int = 0
    wind_down_token_mark: int = 0
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
    message_timestamps: list[str] = field(default_factory=list)
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
            {**msg, "timestamp": ts}
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

    def append_queued_external_user_turn(self, content: str) -> None:
        """Atomically mark and append a public user turn awaiting its driver."""
        pending = {
            "turn_id": uuid.uuid4().hex,
            "status": "queued",
            "content": content,
        }
        self.pending_external_user_turn = pending
        self.append_message({"role": "user", "content": content})
        pending["message_index"] = len(self.messages) - 1

    def consume_queued_external_user_turn(self) -> None:
        """Mark a queued external turn as claimed by the current runner."""
        if self.pending_external_user_turn is not None:
            self.pending_external_user_turn = None

    def start_active_turn(self, message_index: int) -> None:
        """Persist the answer-lookup boundary for an active user turn."""
        self.active_turn_start_message_index = message_index

    def clear_active_turn(self) -> None:
        """Discard a turn boundary once that turn reaches a terminal phase."""
        self.active_turn_start_message_index = None

    @property
    def is_done(self) -> bool:
        return self.phase == SessionPhase.DONE

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.message_timestamps.append(_now_iso())

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Swap the conversation, re-deriving aligned timestamps.

        Precedence per message: an embedded ``timestamp`` (e.g. a resumed
        transcript) wins; else the prior timestamp of the same dict object
        (preserved across a slice-and-rebuild); else now.
        """
        prior = {id(m): ts for m, ts in zip(self.messages, self.message_timestamps)}
        rebuilt = [
            (
                ({k: v for k, v in m.items() if k != "timestamp"}, m["timestamp"])
                if isinstance(m, dict) and "timestamp" in m
                else (m, prior.get(id(m)) or _now_iso())
            )
            for m in messages
        ]
        self.messages = [m for m, _ in rebuilt]
        self.message_timestamps = [ts for _, ts in rebuilt]

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
        # A fresh copy each restore, so the checkpoint stays reusable.
        self.turn = copy.deepcopy(checkpoint.turn)
        self.wind_down_done = checkpoint.wind_down_done
        self.wind_down_attempts = checkpoint.wind_down_attempts
        self.wind_down_token_mark = checkpoint.wind_down_token_mark

    def reset_for_user_turn(self) -> None:
        """Prepare an existing session to accept a new user turn.

        Resets only *per-turn* state: the terminal phase (back to IDLE) and the
        whole per-turn enforcement window. ``step_count`` and ``used_tokens`` are
        intentionally preserved — both ``max_steps`` and ``max_budget_tokens``
        are session-lifetime caps, so a long-lived interactive/messaged session
        keeps accumulating across turns rather than getting a fresh allowance.
        Durable wind-down latches are current-turn state and reset here.
        """
        self.resume_to_idle()
        self.turn = TurnEnforcementState()
        self.wind_down_done = False
        self.wind_down_attempts = 0
        self.wind_down_token_mark = 0

    def record_evidence_signal(
        self,
        content_hash: str,
        call_hash: str,
        intrinsic_low_yield: bool,
        *,
        card: dict[str, Any] | None = None,
    ) -> None:
        """Fold one executed tool result into the information-gain counters (STEP 1)
        and, when ``card`` is given, the evidence ledger (STEP 2).

        ``intrinsic_low_yield`` flags an empty read or a "No matches"-class result
        (low-yield regardless of novelty). A result is INFORMATIVE only when it is
        not intrinsically low-yield AND at least one of its hashes is novel (an
        unseen result content OR an unseen (tool, normalized-args) call key). An
        informative result resets ``low_yield_since_progress`` and increments
        ``distinct_evidence_count``; a low-yield result increments
        ``low_yield_since_progress``. Both hashes are remembered, so a later exact
        re-issue — or a re-read of the same file at a shifted range, caught by the
        path-normalized call hash — scores zero gain. Observational only: STEP 1
        wires no behavior to these counters.

        STEP 2: the SAME novelty + intrinsic decision determines the ledger
        ``outcome`` (``hit`` | ``NO-MATCH`` | ``duplicate``) — no second
        classification. When ``card`` (carrying ``tool`` / ``target`` / ``snippet``
        from the tool-result envelope) is supplied, a completed card is appended to
        ``scout_ledger``. ``card=None`` keeps the pre-STEP-2 path (counters only),
        so callers that do not produce cards are byte-for-byte unchanged.
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
            # Same control flow as STEP 1 (low-yield bumps the counter); only the
            # ledger label distinguishes an intrinsic no-result from a re-seen dup.
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
            self.turn.recent_call_hashes = self.turn.recent_call_hashes[-max_window:]

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.turn.recent_call_hashes = call_hashes
