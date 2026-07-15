from __future__ import annotations

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
# is a *controlled* stop that delivers a clean result to the parent, never an
# unhandled ERROR, so a child that overflows or exhausts its budget does not crash
# the parent's turn.
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
        {SessionPhase.EXECUTING_TOOLS, SessionPhase.DONE, SessionPhase.AUTOSAVING}
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


@dataclass(frozen=True)
class _UserTurnCheckpoint:
    message_count: int
    timestamp_count: int
    phase: SessionPhase
    terminal_reason: str | None
    recent_call_hashes: tuple[str, ...]
    reads_since_last_edit: int
    low_yield_since_progress: int
    distinct_evidence_count: int
    steps_since_progress: int
    loop_blocked_since_progress: int
    seen_result_hashes: frozenset[str]
    scout_ledger: tuple[dict[str, Any], ...]


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
    recent_call_hashes: list[str] = field(default_factory=list)
    # Closed-loop steering signal: how many read-only tool calls (file_read/grep)
    # have run since the last successful edit. Drives the reads-without-write
    # nudge/escalation in the steering block. Per-turn like ``recent_call_hashes``
    # (reset by ``reset_for_user_turn``); reset to 0 on a successful write.
    reads_since_last_edit: int = 0
    # Enforcement wind-down (STEP 0). ``wind_down_done`` flips True the first time
    # the enforcement precheck forces a scout into its single protected submit
    # turn; it is the latch that grants EXACTLY ONE more CALLING_LLM and then
    # routes to a terminal. ``wind_down_token_mark`` records ``used_tokens`` at the
    # trip so the commitment-terminus metric can report ``submit_turn_cost`` (the
    # tokens the protected submit turn spent). Both are inert unless
    # ``enforcement_strength`` is on, so a self-regulating run never touches them.
    wind_down_done: bool = False
    wind_down_token_mark: int = 0
    # Information-gain sensor (STEP 1). A purely OBSERVATIONAL trio maintained on
    # every executed tool result regardless of ``enforcement_strength`` (cheap; it
    # feeds later brakes but adds NO control flow itself yet). A result is
    # "informative" when its content is NOVEL — neither its content hash nor its
    # path-normalized (tool, args) call hash has been seen before — AND it is not
    # an empty read or a "No matches"-class result; otherwise it is "low-yield".
    # ``low_yield_since_progress`` counts consecutive low-yield results since the
    # last informative one (reset to 0 on any informative result);
    # ``distinct_evidence_count`` tallies informative results in the current turn.
    # ``_seen_result_hashes`` is the novelty memory (holds both content and call
    # hashes). Keying on NOVELTY (not the literal "No matches" string) means a
    # model re-reading a known file at a shifted range to dodge a string match
    # still scores zero gain. All three reset on a fresh user turn, like the
    # sibling per-turn signals ``reads_since_last_edit`` / ``recent_call_hashes``.
    low_yield_since_progress: int = 0
    distinct_evidence_count: int = 0
    _seen_result_hashes: set[str] = field(default_factory=set)
    # Harness-authored evidence ledger (STEP 2). A deterministic, ALWAYS-ON
    # durable capture FLOOR: one compact card ``{tool, target, outcome, snippet}``
    # appended per EXECUTED tool result, built purely from the tool-result envelope
    # (no model involvement, so capture can never fail). ``outcome`` REUSES the
    # STEP-1 classification — ``hit`` (novel informative), ``NO-MATCH`` (empty /
    # "No matches"-class intrinsic low-yield), ``duplicate`` (seen-before content or
    # path-normalized call). The harvest backstop reads this ledger to salvage a
    # chopped scout, and the dead-scout synthesizer feeds it back to the model. Like
    # the sibling observational counters it is per-turn (reset by
    # ``reset_for_user_turn``) and adds NO control flow of its own.
    scout_ledger: list[dict[str, Any]] = field(default_factory=list)
    # Progress watchdog (STEP 3). ``steps_since_progress`` counts consecutive
    # tool-executing STEPS (batches) that produced NO progress, where progress is a
    # landed write OR a novel informative ("hit") result — the same signal the
    # STEP-1 sensor / STEP-2 ledger record. It resets to 0 on any progress step and
    # increments by 1 per no-progress tool batch. Maintained ALWAYS (cheap,
    # observational, like the STEP-1 counters); only the precheck BRAKE that reads
    # it is gated behind enforcement, so a self-regulating run is byte-for-byte
    # unchanged. The watchdog brake trips when it reaches K even with budget
    # remaining (catches spin while budget is plentiful). Per-turn — reset by
    # ``reset_for_user_turn`` alongside its sibling sensor counters.
    steps_since_progress: int = 0
    # Anti-windup latch (STEP 3). Set True when a forced-commit turn (wind-down /
    # watchdog / low-yield brake) was issued but the agent did NOT commit on it and
    # the loop had to escalate to its single retry. Observability only — the actual
    # escalation is the runner's ``_wind_down_retried`` latch; this records that the
    # physical tool-removal actuator was not satisfied on the first forced turn. A
    # session-lifetime latch like ``wind_down_done`` (not reset per user turn).
    forced_unsatisfied: bool = False
    # Hard loop-block brake. Tool execution short-circuits repeated identical or
    # path-normalized calls; this counter lets that short-circuit stop a session
    # after repeated warnings instead of paying for more near-identical turns.
    loop_blocked_since_progress: int = 0
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

    def __post_init__(self) -> None:
        self._align_timestamps()

    def _align_timestamps(self) -> None:
        if len(self.message_timestamps) < len(self.messages):
            now = _now_iso()
            self.message_timestamps += [now] * (
                len(self.messages) - len(self.message_timestamps)
            )

    def enriched_messages(self) -> list[dict[str, Any]]:
        """Messages with their creation ``timestamp`` merged in, for persistence.

        Re-aligns first so messages appended by bypassing ``append_message``
        (e.g. direct list mutation) still get a timestamp.
        """
        self._align_timestamps()
        return [
            {**msg, "timestamp": ts}
            for msg, ts in zip(self.messages, self.message_timestamps)
        ]

    def queue_pending_user_message(self, message: dict[str, Any]) -> None:
        self.pending_user_messages.append({**message, "timestamp": _now_iso()})

    def discard_pending_user_message(self, content: str) -> None:
        for index, message in enumerate(self.pending_user_messages):
            if message.get("content") == content:
                del self.pending_user_messages[index]
                return

    def enriched_pending_user_messages(self) -> list[dict[str, Any]]:
        return [dict(message) for message in self.pending_user_messages]

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
            recent_call_hashes=tuple(self.recent_call_hashes),
            reads_since_last_edit=self.reads_since_last_edit,
            low_yield_since_progress=self.low_yield_since_progress,
            distinct_evidence_count=self.distinct_evidence_count,
            steps_since_progress=self.steps_since_progress,
            loop_blocked_since_progress=self.loop_blocked_since_progress,
            seen_result_hashes=frozenset(self._seen_result_hashes),
            scout_ledger=tuple(dict(card) for card in self.scout_ledger),
        )

    def restore_user_turn(self, checkpoint: _UserTurnCheckpoint) -> None:
        """Roll back an interrupted user-message append to its commit boundary."""
        del self.messages[checkpoint.message_count :]
        del self.message_timestamps[checkpoint.timestamp_count :]
        self.phase = checkpoint.phase
        self.terminal_reason = checkpoint.terminal_reason
        self.recent_call_hashes = list(checkpoint.recent_call_hashes)
        self.reads_since_last_edit = checkpoint.reads_since_last_edit
        self.low_yield_since_progress = checkpoint.low_yield_since_progress
        self.distinct_evidence_count = checkpoint.distinct_evidence_count
        self.steps_since_progress = checkpoint.steps_since_progress
        self.loop_blocked_since_progress = checkpoint.loop_blocked_since_progress
        self._seen_result_hashes = set(checkpoint.seen_result_hashes)
        self.scout_ledger = [dict(card) for card in checkpoint.scout_ledger]

    def reset_for_user_turn(self) -> None:
        """Prepare an existing session to accept a new user turn.

        Resets only *per-turn* state: the terminal phase (back to IDLE) and the
        loop-detection hashes. ``step_count`` and ``used_tokens`` are
        intentionally preserved — both ``max_steps`` and ``max_budget_tokens``
        are session-lifetime caps, so a long-lived interactive/messaged session
        keeps accumulating across turns rather than getting a fresh allowance.
        """
        self.resume_to_idle()
        self.clear_recent_tool_hashes()
        self.reads_since_last_edit = 0
        self.low_yield_since_progress = 0
        self.distinct_evidence_count = 0
        self.steps_since_progress = 0
        self.loop_blocked_since_progress = 0
        self._seen_result_hashes.clear()
        self.scout_ledger.clear()

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
            content_hash in self._seen_result_hashes
            or call_hash in self._seen_result_hashes
        )
        self._seen_result_hashes.add(content_hash)
        self._seen_result_hashes.add(call_hash)
        if not intrinsic_low_yield and not already_seen:
            self.low_yield_since_progress = 0
            self.distinct_evidence_count += 1
            outcome = "hit"
        else:
            # Same control flow as STEP 1 (low-yield bumps the counter); only the
            # ledger label distinguishes an intrinsic no-result from a re-seen dup.
            self.low_yield_since_progress += 1
            outcome = "NO-MATCH" if intrinsic_low_yield else "duplicate"
        if card is not None:
            self.scout_ledger.append(
                {
                    "tool": card.get("tool", ""),
                    "target": card.get("target", ""),
                    "outcome": outcome,
                    "snippet": card.get("snippet", ""),
                }
            )
            if len(self.scout_ledger) > MAX_SCOUT_LEDGER_CARDS:
                del self.scout_ledger[:-MAX_SCOUT_LEDGER_CARDS]

    def remember_tool_call_hash(self, call_hash: str, max_window: int | None = None) -> None:
        self.recent_call_hashes.append(call_hash)
        if max_window is not None and len(self.recent_call_hashes) > max_window:
            self.recent_call_hashes = self.recent_call_hashes[-max_window:]

    def clear_recent_tool_hashes(self) -> None:
        self.recent_call_hashes.clear()

    def replace_recent_tool_hashes(self, call_hashes: list[str]) -> None:
        self.recent_call_hashes = call_hashes
