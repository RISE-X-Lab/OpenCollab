from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    CompletionResponse,
    EventPublisherPort,
    LLMPort,
    ShaperPort,
    TracePort,
)
from opencollab.application.extension_valve import (
    EXTENSION_DENIED_NUDGE,
    EXTENSION_GRANTED_NUDGE,
    EXTENSION_OFFER_NUDGE,
    judge_extension_reason,
)
from opencollab.application.shaping import forced_shape
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState

logger = logging.getLogger(__name__)

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})


class _ContextOverflowStop(Exception):
    """Internal control-flow signal: the prompt still overflows the model window
    after a forced maximal compaction pass and one retry. Caught inside
    ``run_llm_call`` to perform a controlled graceful stop (CONTEXT_OVERFLOW)
    rather than letting it propagate as an unhandled ERROR. Never escapes the
    use case.
    """


@dataclass
class PendingStep:
    """The LLM response carried across HANDLING_RESPONSE → EXECUTING_TOOLS →
    AUTOSAVING. Lives here, not in domain ``SessionState``: the response is an
    adapter-shaped object and would breach the inward dependency rule.
    """

    response: CompletionResponse
    latency: float


# Re-prompt used when a turn returns neither text nor a tool call (an
# "empty-stop"). Without this the session would silently transition to DONE,
# recording a clean completion that produced no answer and took no action.
_EMPTY_STOP_NUDGE = (
    "Your previous response was empty — no message and no tool call. "
    "If the task is finished, say so briefly. Otherwise continue now by "
    "calling the appropriate tool or giving your answer."
)

# Stand-in for the empty assistant turn, inserted before the nudge so the retry
# request never sends two consecutive user messages (Anthropic rejects that).
# Filtered out of ``run_loop``'s answer scan so it is never mistaken for output.
_EMPTY_STOP_PLACEHOLDER = "[no output produced this turn]"

# Closed-loop steering: a lean per-turn block (budget self-awareness + a
# reads-without-write escalation) injected each turn. At the start of a turn it
# is folded into the trailing user message IN PLACE, so the budget the model saw
# is saved to the transcript; on continuation steps it rides in the shaped copy
# only (ephemeral). Soft: advise a write; hard: demand it + force a tool call.
_READ_TOOLS = frozenset({"file_read", "grep"})
_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
READS_NUDGE_SOFT = 8
READS_NUDGE_HARD = 16

# Enforcement strength (STEP 0). ``off`` is the self-regulating default: every
# wind-down branch below is gated on enforcement being on, so with ``off`` the
# FSM is byte-for-byte identical to the pre-enforcement code. ``needs-enforcement``
# turns on the structural commit brake for budget-myopic models.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_ON = "needs-enforcement"

# Default name of the structured submit tool the wind-down narrows the toolset to
# (overridable per session). Kept as a string so this application module need not
# import the tool itself.
SUBMIT_TOOL_NAME = "submit_findings"

# Reserve (carved FROM the cap, never additive) held back so the single protected
# submit turn fits: ``explore_threshold = max_budget_tokens - DEFAULT_COMMIT_RESERVE``.
DEFAULT_COMMIT_RESERVE = 25_000

# Progress watchdog + low-yield brake (STEP 3). Two ADDITIONAL triggers into the
# SAME forced-commit actuator (``_enter_wind_down``) — the budget-threshold
# wind-down is the first. Both catch a scout spinning while budget is still
# plentiful (the real pathology) and route it to the non-degradable tool-removal
# commit. They key STRICTLY on the STEP-1 information-gain sensor (consecutive
# no-progress steps / low-yield results), never on ``has_write`` — which is
# overloaded as "agent mutates the repo" and would leak into verify/diff/integrity
# accounting if reused as the brake key.
#
# WATCHDOG_K: ``steps_since_progress`` (no-progress tool STEPS) reaching this trips
# the brake. LOW_YIELD_M: ``low_yield_since_progress`` (consecutive low-yield tool
# RESULTS) reaching this trips it. Both default conservative so a normal
# distill-as-you-read scout is never braked mid-stride.
DEFAULT_WATCHDOG_K = 4
DEFAULT_LOW_YIELD_M = 3

# Predictive overshoot guard (STEP 4a). The agreed ~80% wind-down trips on
# ``used_tokens >= explore_threshold`` — but the very turn meant to submit can BE
# the single-turn overshoot that gets chopped. So we maintain an EWMA of per-turn
# token cost (the post-call ``add_used_tokens`` deltas) and trip the wind-down ONE
# turn EARLY when ``used_tokens + ewma_turn_cost`` would breach the threshold, so
# the protected submit turn still fits inside the reserve.
#
# DEFAULT_EWMA_ALPHA: smoothing factor (recent turns weighted more); the first
# sample seeds the EWMA. The EWMA's influence is CAPPED (``predictive_cap``,
# defaulting to ``commit_reserve``) so one anomalous expensive turn cannot wind the
# scout down far too early — the cap is also the DEADLINE BAND: the predictive term
# can only flip the trigger once ``used_tokens >= explore_threshold - cap``, i.e.
# near the deadline, never across the whole run.
DEFAULT_EWMA_ALPHA = 0.3

# STEP 2C (Phase 2) — predictive-overshoot RELAXATION. The bare guard tripped
# whenever ``used + ewma_term >= explore_threshold`` (i.e. whenever the predicted
# next turn would merely *reach* the threshold), which severed a live lead with
# 6-10k of slack still left even though that turn would land near the threshold and
# leave nearly the full reserve for the submit. The margin shifts the predictive
# trigger to ``>= explore_threshold + margin``: a NORMAL-sized turn (ewma <= margin)
# is no longer pre-empted — the plain ``used >= explore_threshold`` wind-down still
# catches it at the deadline with the full reserve intact — while a turn large
# enough to consume more than ``margin`` of the reserve (the genuine single-turn
# overshoot Step 4 exists to stop) still trips early. The margin is the slice of the
# reserve we are willing to let one straddling exploration turn spend; ``reserve -
# margin`` is therefore guaranteed to the protected submit turn. ``None`` -> 40% of
# ``commit_reserve`` (10k of the default 25k reserve, leaving 15k for the submit).
DEFAULT_PREDICTIVE_MARGIN_FRACTION = 0.4

# Single-justified-extension valve (STEP 4b). Hard cap on bounded extensions a
# scout may earn at a wind-down trip. Exactly one: a genuinely-deep dimension gets
# one more justified read; everything else is force-committed as before.
DEFAULT_MAX_EXTENSIONS = 1

# Injected as a system message the instant a scout crosses the explore threshold.
# Its next action must be the structured submit; cite-or-abstain, never fabricate.
# STEP 2A (Phase 2): the toolset is narrowed to submit-only AT this same trip, so
# the notice states it UP FRONT — without it a DeepSeek-class model reflexively
# re-issues its prior exploration tool (grep/file_read/bash, now unknown) and burns
# a whole turn on the "unknown tool" error before the provider-compat retry
# (``_WIND_DOWN_RETRY``) recovers it. Telling the model here pre-empts that dead
# turn; the retry backstop stays as the second line of defense.
_WIND_DOWN_NUDGE = (
    "Exploration budget spent — all exploration tools (grep/file_read/bash) have now "
    "been removed; ONLY submit_findings is available. Your NEXT action MUST be "
    "submit_findings with the evidence you already have; do not call any other tool. "
    "Cite file:line / exact matched strings from your real tool results; mark each "
    "finding verified|unverified; if you lack evidence for a dimension, set "
    "insufficient_evidence — do NOT fabricate."
)

# Provider-compat backstop (re-injected for the SINGLE wind-down retry). A model
# that ignored the forced tool_choice and reflexively re-issued its prior tool
# (grep/bash/file_read — now unknown, since the toolset is submit-only) gets one
# more turn with this explicit instruction before the loop goes terminal.
_WIND_DOWN_RETRY = (
    "Only submit_findings is available. Call submit_findings now with the evidence you "
    "have; do not call any other tool. Cite file:line / exact matched strings; mark each "
    "finding verified|unverified; set insufficient_evidence if you lack evidence — do NOT "
    "fabricate."
)


def _submit_tool_choice(name: str) -> dict[str, Any]:
    """OpenAI-style named-function ``tool_choice`` forcing exactly ``name``.

    Constrained decoding for the wind-down turn: more precise than the bare
    ``"required"`` string and more likely honoured by stricter OpenAI-compatible
    endpoints. It rides through the LLM stack unchanged (the OpenAI SDK accepts a
    dict ``tool_choice``); if an endpoint still 400-rejects it, ``_complete``
    degrades it ONCE to ``"auto"`` and the wind-down retry (text instruction +
    submit-only toolset) is the remaining guarantee.
    """
    return {"type": "function", "function": {"name": name}}


class SessionRunUseCase:
    """Application use case for the session run loop.

    The LLM response is structural (``CompletionResponse`` in
    ``application/ports.py``): it must expose ``content``, ``tool_calls``,
    ``finish_reason``, ``usage.input_tokens``, and ``usage.total_tokens``.
    """

    def __init__(
        self,
        *,
        agent: Any,
        state: SessionState,
        llm: LLMPort,
        event_publisher: EventPublisherPort,
        event_factory: SessionEventFactory | None = None,
        tool_execution: ToolExecutionUseCase,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 1_000_000,
        max_steps: int = 100,
        deferrable_tool_names: frozenset[str] = DEFAULT_DEFERRABLE_TOOLS,
        shaper: ShaperPort | None = None,
        team_budget_exhausted: Callable[[], bool] | None = None,
        is_context_overflow: Callable[[Exception], bool] | None = None,
        per_call_timeout: float | None = None,
        enforcement_strength: str = ENFORCEMENT_OFF,
        commit_reserve: int = DEFAULT_COMMIT_RESERVE,
        submit_tool_name: str = SUBMIT_TOOL_NAME,
        watchdog_k: int = DEFAULT_WATCHDOG_K,
        low_yield_m: int = DEFAULT_LOW_YIELD_M,
        ewma_alpha: float = DEFAULT_EWMA_ALPHA,
        predictive_cap: int | None = None,
        predictive_margin: int | None = None,
        max_extensions: int = DEFAULT_MAX_EXTENSIONS,
        extension_tool: Any | None = None,
    ):
        self.agent = agent
        self.state = state
        self.llm = llm
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(state.aid)
        self.tool_execution = tool_execution
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self.deferrable_tool_names = deferrable_tool_names
        self.shaper = shaper
        # Defense-in-depth aggregate ceiling. An injected zero-arg predicate that
        # reports whether the *team total* spend has reached the global cap.
        # Passed as a plain callable (resolved in bootstrap/the factory) so the
        # application layer never imports the concrete Scheduler — same pattern as
        # ``max_budget_tokens`` being injected rather than read from the scheduler.
        # ``None`` for standalone (non-team) sessions: only the per-session cap
        # applies, preserving existing behavior.
        self._team_budget_exhausted = team_budget_exhausted
        # Adapter-supplied predicate classifying a provider exception as a
        # context-window overflow (prompt too large even after reactive
        # compaction). Injected as a plain callable (resolved in the factory) so
        # the application layer never imports ``adapters.llm`` — same boundary
        # pattern as ``team_budget_exhausted``. ``None`` (tests/standalone)
        # means "never an overflow", preserving the prior propagate-as-ERROR
        # behaviour for callers that don't wire it.
        self._is_context_overflow = is_context_overflow or (lambda _exc: False)
        # Hard ceiling (seconds) on a SINGLE model generation. The provider's own
        # httpx ``request_timeout`` does not bound a slow streaming/thinking
        # generation cooperatively; this wraps the call in ``asyncio.wait_for`` so
        # one ~595s generation cannot consume the whole run wall (P7). ``None``
        # disables it, preserving prior behavior for callers that don't wire it.
        self._per_call_timeout = per_call_timeout
        self._pending: PendingStep | None = None
        # Guards the once-per-session retry on an empty-stop turn (see
        # ``handle_pending_response``).
        self._empty_stop_retried = False
        # High-water mark of the steering nudge level emitted so far
        # (None|'soft'|'hard'). Drives _maybe_trace_steering to log only UPWARD
        # crossings, re-arming on a write reset. Never persisted.
        self._last_steering_level: str | None = None
        # Enforcement wind-down (STEP 0). Off by default: when ``off`` the precheck
        # wind-down branch is never taken, so the FSM is byte-for-byte identical to
        # the pre-enforcement code. ``commit_reserve`` is carved FROM the cap (not
        # additive), so the single protected submit turn fits inside the budget.
        self._enforcement_strength = enforcement_strength
        self._commit_reserve = commit_reserve
        self._submit_tool_name = submit_tool_name
        # Progress watchdog + low-yield brake knobs (STEP 3). Inert unless
        # ``enforcement_strength`` is on (the brake gate); they then route a
        # spinning scout into the SAME ``_enter_wind_down`` actuator the budget
        # threshold uses.
        self._watchdog_k = watchdog_k
        self._low_yield_m = low_yield_m
        # Predictive overshoot guard (STEP 4a). The EWMA of per-turn token cost,
        # folded post-call from ``add_used_tokens`` deltas. Always maintained
        # (cheap, observational); only the precheck predictive TRIGGER that reads it
        # is gated behind enforcement, so a self-regulating run is byte-for-byte
        # unchanged. ``predictive_cap`` (None -> use ``commit_reserve``) caps the
        # EWMA's influence and defines the deadline band.
        self._ewma_alpha = ewma_alpha
        self._predictive_cap = predictive_cap
        # STEP 2C (Phase 2). The slack the predictive guard tolerates before tripping
        # (``None`` -> 40% of ``commit_reserve``). On the enforcement path only; with
        # enforcement off ``_predictive_overshoot`` is never consulted, so this knob
        # cannot affect the reference (off==reference) behavior.
        self._predictive_margin = predictive_margin
        self._ewma_turn_cost = 0.0
        # Single-justified-extension valve (STEP 4b). ``_extension_tool`` is the
        # ``request_extension`` capture tool injected ONLY at the offer turn (never
        # in the scout's normal toolset). The valve is inert unless this is wired
        # (so STEP 0/3 sessions, which carry no such tool, are unchanged) AND
        # enforcement is on. ``_max_extensions`` is the hard cap.
        self._max_extensions = max_extensions
        self._extension_tool = extension_tool
        # Snapshot of the scout's exploration toolset taken at an offer trip, so a
        # GRANT can restore it verbatim for the single extra read turn.
        self._scout_tools_snapshot: list[Any] | None = None
        # Guards the once-per-session provider-compat retry on the wind-down turn
        # (a model that ignored the forced tool_choice and called another/unknown
        # tool gets exactly ONE more turn before going terminal).
        self._wind_down_retried = False

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        """Drive the phase FSM until the turn finishes or suspends.

        Returns the last assistant text answer (empty string if none). On an
        unexpected exception the session is marked failed and the exception
        re-raised; cancellation flushes the tracer before propagating.
        """
        try:
            # A session suspended on deferred work resumes from its pending
            # table; a completed turn (DONE) short-circuits to its last answer;
            # an aborted turn (cancelled/budget/error) resumes to IDLE so a bare
            # re-run continues. resume_to_idle no-ops on non-terminal phases.
            if self.state.phase is SessionPhase.AWAITING_EVENTS:
                self._resume_from_awaiting()
            elif self.state.phase is not SessionPhase.DONE:
                self.state.resume_to_idle()
                # A fresh turn (or re-run) restores the once-per-turn empty-stop
                # retry budget. A deferred-resume (AWAITING_EVENTS, above) is the
                # same turn and must keep the flag as-is.
                self._empty_stop_retried = False
            while not self._should_suspend():
                await self.advance(cancel_event)

        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception as exc:
            self.state.fail(reason=f"{type(exc).__name__}: {exc}")
            raise

        for msg in reversed(self.state.messages):
            content = msg.get("content")
            if msg["role"] == "assistant" and content and content != _EMPTY_STOP_PLACEHOLDER:
                return content
        return ""

    def is_terminal_phase(self) -> bool:
        """Whether the session has finished its turn (done/failed/limits)."""
        return self.state.phase.is_terminal()

    def _should_suspend(self) -> bool:
        """The loop stops on a terminal phase (turn finished) OR on the
        non-terminal AWAITING_EVENTS suspend state (turn paused on deferred
        work; the scheduler re-activates it when the events arrive).
        """
        return self.is_terminal_phase() or self.state.phase is SessionPhase.AWAITING_EVENTS

    def _resume_from_awaiting(self) -> None:
        """Drain a complete pending table back into the message history as one
        contiguous tool-result block, then resume at PRECHECK. Defensive no-op
        if woken while still incomplete (the loop guard then stops at once).
        """
        table = self.state.pending_events
        if not table.is_complete():
            return
        for message in table.ordered_results():
            self.state.append_message(message)
        table.clear()
        self.state.transition_to(SessionPhase.PRECHECK)

    async def advance(self, cancel_event: asyncio.Event | None = None) -> None:
        """Execute one FSM step: dispatch the current phase to its handler."""
        match self.state.phase:
            case SessionPhase.SCHEDULED:
                self.state.transition_to(SessionPhase.IDLE)
            case SessionPhase.IDLE:
                self.state.transition_to(SessionPhase.PRECHECK)
            case SessionPhase.PRECHECK:
                await self.precheck(cancel_event)
            case SessionPhase.CALLING_LLM:
                await self.run_llm_call()
            case SessionPhase.HANDLING_RESPONSE:
                await self.handle_pending_response()
            case SessionPhase.EXECUTING_TOOLS:
                await self.execute_pending_tools()
            case SessionPhase.AWAITING_EVENTS:
                # Never dispatched: the loop guard suspends before reaching here.
                self.state.fail()
                raise RuntimeError("advance() called while AWAITING_EVENTS")
            case SessionPhase.AUTOSAVING:
                await self.autosave_pending_step()
            case _:
                self.state.fail()
                raise RuntimeError(f"Cannot advance terminal phase: {self.state.phase.value}")

    def configure_enforcement(
        self,
        *,
        enforcement_strength: str,
        commit_reserve: int | None = None,
        extension_tool: Any | None = None,
        max_extensions: int | None = None,
    ) -> None:
        """Turn the enforcement wind-down on/off post-construction.

        The workflow engine calls this on the scout's session (the agent already
        carries the submit tool) instead of threading the flag through every
        factory layer. ``commit_reserve`` is carved FROM the cap, never added.
        ``extension_tool`` (STEP 4b) arms the single-justified-extension valve: the
        ``request_extension`` capture tool, injected only at the offer turn. Leaving
        it ``None`` keeps the valve off (the wind-down force-commits as in STEP 0/3).
        """
        self._enforcement_strength = enforcement_strength
        if commit_reserve is not None:
            self._commit_reserve = commit_reserve
        if extension_tool is not None:
            self._extension_tool = extension_tool
        if max_extensions is not None:
            self._max_extensions = max_extensions

    def _enforcement_on(self) -> bool:
        return self._enforcement_strength != ENFORCEMENT_OFF

    def _brake_on(self) -> bool:
        """DISTINCT gate for the STEP-3 watchdog / low-yield brakes.

        Deliberately a separate predicate from the ``has_write``-driven write-nudge
        in ``_build_steering_block``: the write-nudge keys on repo-mutation
        (``has_write``) and is disabled for read-only scouts, whereas these brakes
        MUST fire for scouts. They are gated on enforcement (on only for scouts) and
        keyed on the STEP-1 information-gain sensor — never on ``has_write``, whose
        flip would leak into verify/diff/integrity accounting.
        """
        return self._enforcement_on()

    def _enter_wind_down(self) -> None:
        """Trip the single protected submit turn: latch the flag, mark the token
        cost baseline, narrow the toolset to submit-only, FORCE ``tool_choice`` to
        the submit function (constrained decoding — without it DeepSeek-class models
        reflexively re-issue their prior tool, now unknown, and are chopped on the
        error), and inject the commit instruction. The agent is per-session here, so
        mutating its tools / tool_choice cannot leak across sessions. If the submit
        tool is somehow absent the toolset is left as-is (the injected message + the
        one allowed turn still apply)."""
        self.state.wind_down_done = True
        self.state.wind_down_token_mark = self.state.used_tokens
        finder = getattr(self.agent, "find_tool", None)
        submit = finder(self._submit_tool_name) if callable(finder) else None
        if submit is not None:
            self.agent.tools = [submit]
            # Force the submit function specifically (not "auto"/"required"). Persists
            # for the retry turn too; ``_complete`` degrades it once to "auto" on a
            # 400, after which the retry's text instruction is the guarantee.
            self.agent.tool_choice = _submit_tool_choice(self._submit_tool_name)
        self.state.append_message({"role": "system", "content": _WIND_DOWN_NUDGE})

    def _trace_brake_trip(
        self,
        budget_spent: bool,
        predicted_spent: bool,
        watchdog_tripped: bool,
        low_yield_tripped: bool,
    ) -> None:
        """Trace which trigger routed the scout into the forced-commit actuator
        (STEP 3 + STEP 4a). No-op without a tracer; only reachable with enforcement
        on, so the reference (off) path is untouched. ``trigger`` is the
        highest-signal cause so the STEP-6 control-health scorecard can attribute
        spin-brakes vs the budget threshold vs the predictive early-trip (the
        watchdog/low-yield triggers catch spin; ``predictive`` catches the
        single-turn overshoot blind spot)."""
        if self.tracer is None:
            return
        if budget_spent:
            trigger = "budget"
        elif predicted_spent:
            trigger = "predictive"
        elif watchdog_tripped:
            trigger = "watchdog"
        else:
            trigger = "low_yield"
        self.tracer.log_step(
            step_type="commit_brake",
            payload={
                "aid": self.state.aid,
                "trigger": trigger,
                "budget_spent": budget_spent,
                "predicted_spent": predicted_spent,
                "watchdog_tripped": watchdog_tripped,
                "low_yield_tripped": low_yield_tripped,
                "ewma_turn_cost": round(self._ewma_turn_cost, 1),
                "steps_since_progress": self.state.steps_since_progress,
                "low_yield_since_progress": self.state.low_yield_since_progress,
                "used_tokens": self.state.used_tokens,
                "step": self.state.step_count,
            },
        )

    def _update_turn_cost_ewma(self, delta: int) -> None:
        """Fold one per-turn token delta into the EWMA used by the predictive
        overshoot guard (STEP 4a). The first positive sample seeds the EWMA; later
        samples are exponentially smoothed. Non-positive deltas are ignored (a
        cached/zero-cost turn must not drag the estimate down). Pure runner state —
        no message/event/persisted-state change, so off==reference is preserved."""
        if delta <= 0:
            return
        if self._ewma_turn_cost <= 0:
            self._ewma_turn_cost = float(delta)
        else:
            a = self._ewma_alpha
            self._ewma_turn_cost = a * float(delta) + (1.0 - a) * self._ewma_turn_cost

    def _predictive_overshoot(self, explore_threshold: int) -> bool:
        """STEP 4a (+ STEP 2C relaxation): would the NEXT turn, at the running per-turn
        EWMA, overshoot the explore threshold by more than the tolerated ``margin``?
        Trips the wind-down one turn early so a genuinely large turn cannot straddle
        the threshold and eat the reserve out from under the protected submit turn.

        The EWMA's influence is capped (``predictive_cap`` / ``commit_reserve``),
        which both bounds how early an anomalous expensive turn can wind the scout
        down AND limits the predictive term to the deadline band. STEP 2C adds the
        ``margin``: the guard fires only when the predicted landing exceeds
        ``explore_threshold + margin`` — so a normal-sized turn (ewma <= margin) is no
        longer pre-empted with comfortable slack (the plain ``used >=
        explore_threshold`` wind-down still catches it at the deadline with the full
        reserve), while a turn big enough to spend more than ``margin`` of the reserve
        still trips early. Equivalently, the guard now fires only when the predicted
        next-turn cost exceeds the slack-to-threshold by more than ``margin``. Inert
        until a per-turn cost has actually been measured. Reached only on the
        enforcement path, so off==reference is preserved regardless of ``margin``."""
        if self._ewma_turn_cost <= 0:
            return False
        cap = self._predictive_cap if self._predictive_cap is not None else self._commit_reserve
        ewma_term = min(self._ewma_turn_cost, float(cap))
        margin = (
            self._predictive_margin
            if self._predictive_margin is not None
            else int(self._commit_reserve * DEFAULT_PREDICTIVE_MARGIN_FRACTION)
        )
        return (self.state.used_tokens + ewma_term) >= explore_threshold + margin

    def _extension_available(self) -> bool:
        """STEP 4b: is the single-justified-extension valve armed for THIS trip?

        Only when a ``request_extension`` capture tool is wired (so STEP 0/3
        sessions without one are byte-for-byte unchanged), no offer is already
        outstanding, and the hard cap has not been reached. Enforcement is already
        checked by the caller (this only runs inside the enforcement block)."""
        return (
            self._extension_tool is not None
            and not self.state.extension_offered
            and self.state.extensions_granted < self._max_extensions
        )

    def _offer_extension(self) -> None:
        """STEP 4b: instead of immediately force-committing, give the scout ONE
        decision turn — commit (submit_findings) OR justify one more read
        (request_extension). Snapshot the exploration toolset (to restore on a
        grant), narrow the toolset to {submit_findings, request_extension}, force a
        tool choice between the two, and inject the offer. The agent is per-session,
        so mutating its tools cannot leak across sessions."""
        self.state.extension_offered = True
        self._scout_tools_snapshot = list(getattr(self.agent, "tools", []) or [])
        finder = getattr(self.agent, "find_tool", None)
        submit = finder(self._submit_tool_name) if callable(finder) else None
        offer_tools = [t for t in (submit, self._extension_tool) if t is not None]
        if offer_tools:
            self.agent.tools = offer_tools
            # Force a choice between the two offered tools (commit or justify). If the
            # provider rejects "required" it degrades to "auto" in ``_complete``; the
            # injected message is then the remaining steer.
            self.agent.tool_choice = "required"
        self.state.append_message({"role": "system", "content": EXTENSION_OFFER_NUDGE})

    def _restore_scout_tools(self) -> None:
        """STEP 4b: on a GRANT, restore the exploration toolset for the single extra
        read turn — minus ``request_extension`` so the granted turn cannot re-request
        (the hard cap also enforces this). Tool choice returns to the provider
        default so the model explores freely for one turn before the re-fired brake
        force-commits it."""
        if self._scout_tools_snapshot is None:
            return
        ext_name = getattr(self._extension_tool, "name", None)
        self.agent.tools = [
            t for t in self._scout_tools_snapshot if getattr(t, "name", None) != ext_name
        ]
        self.agent.tool_choice = None

    def _resolve_extension_offer(self) -> None:
        """STEP 4b: act on the scout's response to an extension offer. Reached at the
        precheck AFTER an offer turn (a committed submit_findings instead sets the
        cancel event and is caught as CANCELLED before this runs). Reads the recorded
        ``request_extension`` reason and judges it: a concrete, falsifiable, NOVEL
        reason GRANTS exactly one bounded extension (restore tools, one more read);
        an absent / vacuous / duplicate reason DENIES it and routes straight to the
        forced submit-only wind-down. Always transitions to CALLING_LLM."""
        self.state.extension_offered = False
        reason = ""
        tool = self._extension_tool
        requested = getattr(tool, "requested", None) if tool is not None else None
        if requested:
            reason = str(requested.get("reason") or "")
            tool.requested = None  # consume; the hard cap is the real limiter
        granted, why = (False, "absent")
        if reason:
            granted, why = judge_extension_reason(reason, self.state.extension_reasons)
        if granted:
            self.state.extensions_granted += 1
            self.state.extension_reasons.append(reason)
            self._trace_extension_decision(reason, granted=True, why=why)
            self._restore_scout_tools()
            self.state.append_message({"role": "system", "content": EXTENSION_GRANTED_NUDGE})
        else:
            self._trace_extension_decision(reason, granted=False, why=why)
            # Denied -> the STEP 0/3 forced-commit actuator (submit-only + forced
            # tool_choice), with the denial reason in the injected nudge.
            self._enter_wind_down()
            self.state.messages[-1] = {"role": "system", "content": EXTENSION_DENIED_NUDGE}
        self.state.transition_to(SessionPhase.CALLING_LLM)

    def _trace_extension_decision(self, reason: str, *, granted: bool, why: str) -> None:
        """Trace one ``extension_decision`` event (STEP 4b) so the STEP-6 scorecard
        can audit grant rate / denial reasons. No-op without a tracer."""
        if self.tracer is None:
            return
        self.tracer.log_step(
            step_type="extension_decision",
            payload={
                "aid": self.state.aid,
                "granted": granted,
                "why": why,
                "reason": (reason or "")[:200],
                "extensions_granted": self.state.extensions_granted,
                "used_tokens": self.state.used_tokens,
                "step": self.state.step_count,
            },
        )

    async def precheck(self, cancel_event: asyncio.Event | None) -> None:
        """Gate the next LLM call: cancellation, token budget, step limit.

        Each guard appends a visible system message, emits an error event, and
        moves to the matching terminal phase; otherwise proceed to CALLING_LLM.
        """
        if cancel_event and cancel_event.is_set():
            self.state.append_message({"role": "system", "content": "[Session interrupted by user]"})
            await self.event_publisher.emit(self.event_factory.error("cancelled"))
            self.state.transition_to(SessionPhase.CANCELLED, reason="interrupted by user")
            return

        # Enforcement wind-down (STEP 0) — gated; with enforcement OFF this whole
        # block is skipped and the budget trip below runs exactly as before. When
        # ON, at ~80% of the cap we force a single structured submit instead of
        # letting the scout get chopped mid-exploration.
        if self._enforcement_on():
            # STEP 4b: an extension OFFER from last turn takes priority — resolve the
            # scout's commit-or-justify choice before any other gate. (A committed
            # submit_findings on the offer turn sets the cancel event and is caught
            # above as CANCELLED, so reaching here means it did not commit.)
            if self.state.extension_offered:
                self._resolve_extension_offer()
                return
            if self.state.wind_down_done:
                # We only reach here when the protected submit turn did NOT commit —
                # a successful capture sets the cancel event and is caught above as
                # CANCELLED. Provider-compat backstop: a model that ignored the
                # forced tool_choice and called another/unknown tool (or emitted an
                # invalid submit) gets EXACTLY ONE more turn — re-stating that only
                # submit_findings is available — before the loop goes terminal. The
                # toolset stays submit-only and tool_choice stays forced from the
                # initial trip, so the retry turn is constrained too. Capped at one
                # retry (no loop); after it the harvest backstop salvages whatever
                # was gathered. terminus only reaches "forced" if submit_findings
                # actually fires (captured) — never recorded off the unknown-tool error.
                if not self._wind_down_retried:
                    self._wind_down_retried = True
                    # Anti-windup (STEP 3): the forced-commit turn was issued but the
                    # agent did NOT commit (a successful capture sets the cancel event
                    # and is caught as CANCELLED above). Latch that the physical
                    # tool-removal actuator went unsatisfied on the first forced turn;
                    # the retry below is the single escalation before terminal.
                    self.state.forced_unsatisfied = True
                    self.state.append_message(
                        {"role": "system", "content": _WIND_DOWN_RETRY}
                    )
                    self.state.transition_to(SessionPhase.CALLING_LLM)
                    return
                reason = "wind-down complete: forced commit within reserve"
                self.state.append_message(
                    {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
                )
                await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
                self.state.transition_to(SessionPhase.BUDGET_EXCEEDED, reason=reason)
                return
            explore_threshold = self.max_budget_tokens - self._commit_reserve
            budget_spent = self.state.used_tokens >= explore_threshold
            # STEP 4a predictive overshoot guard: an ADDITIONAL budget-class trigger
            # that trips ONE turn early when the running per-turn EWMA predicts the
            # next turn would breach the threshold, so the protected submit turn fits
            # inside the reserve instead of being the overshoot that gets chopped.
            predicted_spent = self._predictive_overshoot(explore_threshold)
            # STEP 3 brakes: two ADDITIONAL triggers into the same actuator, keyed on
            # the STEP-1 sensor (never on has_write) via the distinct ``_brake_on``
            # gate. The watchdog catches a scout spinning through no-progress STEPS
            # while budget is still plentiful (the real pathology); the low-yield
            # brake catches a run of zero-gain RESULTS. Routing through
            # ``_enter_wind_down`` makes the terminal rung the non-degradable physical
            # tool-removal + forced tool_choice — provider 'required'→'auto' silent
            # degradation cannot weaken it.
            watchdog_tripped = (
                self._brake_on() and self.state.steps_since_progress >= self._watchdog_k
            )
            low_yield_tripped = (
                self._brake_on() and self.state.low_yield_since_progress >= self._low_yield_m
            )
            brake = budget_spent or predicted_spent or watchdog_tripped or low_yield_tripped
            # RED-TEAM gate: never force-commit while a tool call's result is still
            # un-ingested (pending table non-empty) — that would discard the read
            # that just returned. precheck is normally reached with a drained table.
            if brake and self.state.pending_events.is_empty():
                self._trace_brake_trip(
                    budget_spent, predicted_spent, watchdog_tripped, low_yield_tripped
                )
                # STEP 4b valve: on the FIRST trip with the valve armed, offer
                # commit-or-justify instead of force-committing; otherwise (no valve,
                # offer outstanding, or cap reached) force the submit as in STEP 0/3.
                if self._extension_available():
                    self._offer_extension()
                else:
                    self._enter_wind_down()
                self.state.transition_to(SessionPhase.CALLING_LLM)
                return

        if self.state.used_tokens >= self.max_budget_tokens:
            reason = f"budget exceeded: {self.state.used_tokens} tokens used"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.transition_to(SessionPhase.BUDGET_EXCEEDED, reason=reason)
            return

        # Aggregate ceiling (defense-in-depth): even when this session is under
        # its own cap, stop if the *team total* has reached the global cap. A
        # single overshooting turn or fan-out could otherwise spend past the
        # global pool that reserve-at-allocation is meant to protect.
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            reason = "team budget exceeded: aggregate spend reached the global cap"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.transition_to(SessionPhase.BUDGET_EXCEEDED, reason=reason)
            return

        if self.state.step_count >= self.max_steps:
            reason = f"step limit reached: {self.state.step_count} steps"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("step_limit_exceeded"))
            self.state.transition_to(SessionPhase.STEP_LIMIT_EXCEEDED, reason=reason)
            return

        self.state.transition_to(SessionPhase.CALLING_LLM)

    async def run_llm_call(self) -> None:
        """One model call: track tokens, trace, append the assistant message,
        and stash the response as the pending step for HANDLING_RESPONSE.

        Context-overflow safety net: ``call_llm`` already force-compacts and
        retries once on an overflow rejection. If the retry still overflows it
        raises ``_ContextOverflowStop``; we catch it here and stop the session
        gracefully (CONTEXT_OVERFLOW) instead of letting it crash as an
        unhandled ERROR — mirroring the BUDGET_EXCEEDED degradation.
        """
        self.state.advance_step()
        await self.event_publisher.emit(self.event_factory.step_start(self.state.step_count))
        start = time.monotonic()

        tools = self.build_tool_schemas()
        try:
            response = await self.call_llm(tools)
        except _ContextOverflowStop:
            await self._stop_on_context_overflow()
            return
        latency = time.monotonic() - start
        self.state.add_used_tokens(response.usage.total_tokens)
        # Predictive overshoot guard (STEP 4a): fold this turn's token delta into the
        # per-turn-cost EWMA. Always maintained; the predictive precheck trigger that
        # reads it is the only enforcement-gated consumer.
        self._update_turn_cost_ewma(response.usage.total_tokens)
        self.state.add_markup_recovered(getattr(response.usage, "markup_recovered", 0))
        self.state.set_context_tokens(response.usage.input_tokens)

        self.record_llm_trace(response, latency)
        self.append_assistant_message(response)
        self._pending = PendingStep(response=response, latency=latency)
        self.state.transition_to(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        """Route the pending LLM response: tool calls → EXECUTING_TOOLS;
        a plain text answer finishes the turn (DONE)."""
        pending = self._pending
        if pending is None:
            self.state.fail()
            raise RuntimeError("Cannot handle assistant response before calling LLM")
        response = pending.response

        if response.content:
            await self.event_publisher.emit(self.event_factory.text_delta(response.content))

        if response.tool_calls:
            self.state.transition_to(SessionPhase.EXECUTING_TOOLS)
            return

        # Empty-stop: a clean ``stop`` turn that produced neither text nor a tool
        # call. Falling straight through to DONE would silently record a clean
        # completion that answered nothing. Retry once with a nudge before giving
        # up; the once-per-turn flag (plus the budget/step limits) bounds it. The
        # AUTOSAVING handler finishes the step and loops back to PRECHECK.
        # ``finish_reason`` is gated to "stop": a "length" truncation will only
        # truncate again, so a nudge cannot help there.
        empty_stop = not response.content and not response.tool_calls
        if empty_stop and response.finish_reason in (None, "stop") and not self._empty_stop_retried:
            self._empty_stop_retried = True
            # Record the retry to the trajectory so empty-stops are measurable
            # (the injected nudge/placeholder messages are never persisted).
            if self.tracer:
                self.tracer.log_step(
                    step_type="empty_stop_retry",
                    payload={
                        "finish_reason": response.finish_reason,
                        "had_reasoning": bool(getattr(response, "reasoning", None)),
                    },
                    latency=pending.latency,
                )
            # The empty assistant turn was not appended, so the history may end
            # with a user/tool message. Insert a short assistant placeholder
            # before the nudge so the retry request never sends two consecutive
            # user messages (which Anthropic rejects).
            if self.state.messages and self.state.messages[-1]["role"] in ("user", "tool"):
                self.state.append_message(
                    {"role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER}
                )
            self.state.append_message({"role": "user", "content": _EMPTY_STOP_NUDGE})
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        await self.finish_step(pending.latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.DONE, reason="completed")

    async def execute_pending_tools(self) -> None:
        """Run the pending response's tool calls.

        All-synchronous batches run immediately and proceed to AUTOSAVING. A
        batch containing a deferrable tool (e.g. ``spawn_agent``) is buffered
        whole into the pending-events table so the eventual tool-result block
        stays contiguous; the session then suspends on AWAITING_EVENTS until
        the scheduler delivers the outstanding results.
        """
        pending = self._pending
        if pending is None:
            self.state.fail()
            raise RuntimeError("Cannot execute tools before calling LLM")

        tool_calls = pending.response.tool_calls
        immediate, deferred = self._split_tool_calls(tool_calls)

        # Fast path, unchanged: every tool is synchronous — run them, append
        # results, and autosave the step.
        if not deferred:
            result = await self.tool_execution.process(tool_calls)
            result.apply_to(self.state)
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        # A deferred tool is present. Buffer the WHOLE batch into the pending
        # table so the eventual tool-result block stays contiguous and in the
        # original order, satisfying the LLM rule that every tool_call_id is
        # answered before the next model call.
        table = self.state.pending_events
        order = {tc["id"]: i for i, tc in enumerate(tool_calls)}

        if immediate:
            proc = await self.tool_execution.process(immediate)
            proc.apply_hashes_to(self.state)  # hashes now; messages buffered
            # The reads/edit steering counter must still fold in even though the
            # result MESSAGES are buffered into the pending table (a mixed batch of
            # immediate reads + a deferred spawn would otherwise lose its reads).
            proc.apply_read_write_counter_to(self.state)
            # Likewise the STEP 1 information-gain counters: a buffered immediate
            # result is still a real tool result and must register its novelty.
            proc.apply_evidence_counter_to(self.state)
            for message in proc.messages_to_append:
                tid = message["tool_call_id"]
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.IMMEDIATE,
                        order=order[tid],
                        status=RowStatus.DONE,
                        result=message["content"],
                    )
                )

        for tc in deferred:
            tid = tc["id"]
            ref, error = await self.tool_execution.execute_deferred(tc)
            if ref is not None:
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.CHILD_AGENT,
                        order=order[tid],
                        ref=ref,
                        status=RowStatus.PENDING,
                    )
                )
            else:
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.CHILD_AGENT,
                        order=order[tid],
                        status=RowStatus.FAILED,
                        result=error,
                        error=error,
                    )
                )

        if table.is_complete():
            # Nothing is actually outstanding (e.g. all spawns were rejected
            # synchronously) — drain now and autosave instead of suspending on
            # an event that will never arrive.
            for message in table.ordered_results():
                self.state.append_message(message)
            table.clear()
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        # Genuinely waiting on ≥1 child. Suspend WITHOUT finishing the step:
        # emitting step_end here would autosave a half-open tool-call block (no
        # results yet). The next AUTOSAVING after resume persists a consistent
        # history. tool_start/tool_end + the scheduler's agent_spawned still
        # provide observability of the spawn.
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.AWAITING_EVENTS)

    def _split_tool_calls(self, tool_calls: list[dict]) -> tuple[list[dict], list[dict]]:
        """Partition a batch into (immediate, deferred) by deferrable name."""
        immediate: list[dict] = []
        deferred: list[dict] = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name")
            if name in self.deferrable_tool_names:
                deferred.append(tc)
            else:
                immediate.append(tc)
        return immediate, deferred

    async def autosave_pending_step(self) -> None:
        """Emit step_end (the autosave trigger) and loop back to PRECHECK."""
        pending = self._pending
        latency = pending.latency if pending is not None else 0.0
        await self.finish_step(latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending = None

    def build_tool_schemas(self) -> list[dict] | None:
        return self.agent.tool_schemas() or None

    def _build_steering_block(
        self, messages: list[dict]
    ) -> tuple[dict | None, str | None, str | None]:
        """Build the per-turn closed-loop steering message + any tool_choice force.

        Returns ``(message, tool_choice_override_or_None, level)`` where ``level``
        is ``'hard'``/``'soft'``/``None`` — the trace seam reads it to log upward
        crossings. The message is a lean ``role:"user"`` block carrying budget
        self-awareness plus, when the session can edit and has read without
        writing, a write nudge (soft) or a hard demand (with
        ``tool_choice="required"``). The message is always built; on a fresh
        post-user turn ``reads_since_last_edit`` is ~0 so only the status line is
        returned, which is correct.

        The caller folds this content into ``state.messages``' last message when
        the history ends with a ``user`` message (the start of a turn), persisting
        the budget to the transcript without adding a message; on a continuation
        step (non-user tail) it rides in the shaped copy only. A fresh block is
        rebuilt each turn from the live counters either way.
        """
        total = self.max_budget_tokens or 0
        remaining_k = max(0, total - self.state.used_tokens) // 1000
        total_k = total // 1000
        steps_left = max(0, self.max_steps - self.state.step_count)
        status = f"[Budget: ~{remaining_k}k/{total_k}k tokens left, ~{steps_left} steps left.]"

        reads = self.state.reads_since_last_edit
        has_write = any(
            getattr(t, "name", None) in _WRITE_TOOLS
            for t in getattr(self.agent, "tools", []) or []
        )
        override: str | None = None
        level: str | None = None
        extra = ""
        if has_write and reads >= READS_NUDGE_HARD:
            extra = (
                f" You have read {reads} times without making an edit. STOP reading"
                " — your next action MUST be a file_write or apply_patch edit."
            )
            override = "required"
            level = "hard"
        elif has_write and reads >= READS_NUDGE_SOFT:
            extra = (
                f" You have read {reads} times without making an edit. If"
                " you can describe the fix, make it now with file_write or"
                " apply_patch before reading more."
            )
            level = "soft"
        return {"role": "user", "content": status + extra}, override, level

    def _fold_steering(self, last_user_msg: dict, steering_text: str) -> dict:
        """Return a copy of a trailing ``user`` message with the steering line
        folded into its content (string concat, or content-part append for the
        provider list form). The original dict is not mutated.
        """
        content = last_user_msg.get("content")
        if isinstance(content, str):
            folded = f"{content}\n\n{steering_text}" if content else steering_text
        elif isinstance(content, list):
            folded = [*content, {"type": "text", "text": steering_text}]
        else:
            folded = steering_text
        return {**last_user_msg, "content": folded}

    async def call_llm(self, tools: list[dict] | None) -> CompletionResponse:
        """Complete against the shaped view of history.

        The shaper reshapes a copy for the model's view only;
        ``state.messages`` stays the complete, persisted history.

        Context-overflow safety net: the normal shaped call uses the estimate-
        gated reactive layers, which can under-count dense content (code / JSON /
        CJK) and let a prompt overflow the real window. If the provider rejects
        the call as a context overflow, run a FORCED maximal compaction pass
        (compact every sheddable source unconditionally toward target,
        regardless of the estimate) and retry ONCE. If that *still* overflows —
        e.g. the pinned identity/team/task seed alone exceeds the window — raise
        ``_ContextOverflowStop`` so the caller stops the session gracefully.
        """
        # Closed-loop steering: build a fresh per-turn block from the live
        # counters. When history ends on a USER turn (the start of a turn) the
        # block is folded into that message IN PLACE — no new message, so indices
        # and the timestamp sidecar are unchanged — and thus SAVED to the
        # transcript. On a continuation step the tail is a tool/assistant message
        # with no user turn to fold into, so the block rides in the shaped copy
        # only: the model still sees it, but it stays out of the persisted history.
        steering, tool_choice_override, steering_level = self._build_steering_block(
            self.state.messages
        )
        self._maybe_trace_steering(steering_level)
        persisted = (
            steering is not None
            and bool(self.state.messages)
            and self.state.messages[-1].get("role") == "user"
        )
        if persisted:
            self.state.messages[-1] = self._fold_steering(
                self.state.messages[-1], steering["content"]
            )
        messages = (
            self.shaper.shape(self.state.messages)
            if self.shaper is not None
            else self.state.messages
        )
        if steering is not None and not persisted:
            messages = [*messages, steering]
        try:
            return await self._complete(messages, tools, tool_choice_override)
        except Exception as exc:
            if not self._is_context_overflow(exc):
                raise

        # First overflow: force a maximal compaction pass and retry once.
        forced = (
            forced_shape(self.shaper, self.state.messages)
            if self.shaper is not None
            else self.state.messages
        )
        # No FRESH steering on the emergency-shrink retry: this path is fighting
        # for token space. Any budget folded into a trailing user turn already
        # rides along in history; no new block is added here.
        logger.warning(
            "context overflow on aid=%s: recompacting (%d -> %d messages) and retrying once",
            self.state.aid,
            len(messages),
            len(forced),
        )
        await self.event_publisher.emit(self.event_factory.error("context_overflow_recompacted"))
        try:
            return await self._complete(forced, tools)
        except Exception as exc:
            if self._is_context_overflow(exc):
                raise _ContextOverflowStop() from exc
            raise

    async def _complete(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice_override: str | None = None,
    ) -> CompletionResponse:
        # ``tool_choice`` is read defensively (getattr) so duck-typed agent stubs
        # without the field keep working; ``None`` (the default) keeps the
        # provider's "auto", so the surface is unchanged for ordinary agents.
        # When an agent requests "required" (the forced-write coder) but the
        # provider rejects it (some OpenAI-compatible endpoints don't support
        # it), fall back ONCE to "auto" — the explicit forced prompt and the
        # diff-gate still push the model to write.
        # ``tool_choice_override`` (the steering hard-rung) wins when set, so a
        # read-without-write escalation can force a tool call this turn.
        tool_choice = tool_choice_override or getattr(self.agent, "tool_choice", None)
        try:
            return await self._complete_with_choice(messages, tools, tool_choice)
        except Exception as exc:
            if tool_choice in (None, "auto") or self._is_context_overflow(exc):
                raise
            # Only degrade to "auto" when the provider plainly REJECTED the
            # parameter — a 4xx request-validation error, or a message naming
            # tool_choice. A transient/auth/server error would fail "auto"
            # identically, so re-raise it instead of masking a real fault behind
            # a second call. Duck-typed so the application layer needs no
            # provider-specific exception imports.
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            msg = str(exc).lower()
            if status != 400 and "tool_choice" not in msg and "tool choice" not in msg:
                raise
            logger.warning(
                "tool_choice=%r rejected on aid=%s (%s); retrying once with 'auto'",
                tool_choice,
                self.state.aid,
                type(exc).__name__,
            )
            return await self._complete_with_choice(messages, tools, "auto")

    async def _complete_with_choice(
        self, messages: list[dict], tools: list[dict] | None, tool_choice: str | None
    ) -> CompletionResponse:
        # ``thinking`` is read defensively (getattr) so duck-typed agent stubs
        # without the field keep working. When OFF (the default) the call is made
        # exactly as before — the thinking kwargs are omitted entirely so the LLM
        # surface is byte-for-byte unchanged for every existing caller.
        #
        # ``tool_choice`` is included ONLY when set: an ordinary agent
        # (tool_choice=None) calls ``complete`` with exactly the prior kwargs, so
        # duck-typed LLM stubs that never added the param keep working; only a
        # forcing agent passes it through.
        extra: dict[str, Any] = {}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        # ``top_p`` is read defensively and included ONLY when set, mirroring
        # ``tool_choice``: an agent without it (or with top_p=None) calls
        # ``complete`` with exactly the prior kwargs, so duck-typed LLM stubs that
        # never added the param keep working and the request is unchanged.
        top_p = getattr(self.agent, "top_p", None)
        if top_p is not None:
            extra["top_p"] = top_p
        if not getattr(self.agent, "thinking", False):
            return await self._invoke_llm(
                messages=messages,
                tools=tools,
                temperature=self.agent.temperature,
                **extra,
            )
        return await self._invoke_llm(
            messages=messages,
            tools=tools,
            temperature=self.agent.temperature,
            thinking=True,
            thinking_params=getattr(self.agent, "thinking_params", None),
            **extra,
        )

    async def _invoke_llm(self, **kwargs: Any) -> CompletionResponse:
        """Call the provider, bounding a single generation by ``_per_call_timeout``.

        Without a per-call ceiling one slow generation (a 595s thinking turn was
        observed) can consume the entire run wall, so the outer
        ``asyncio.wait_for`` on the whole workflow truncates everything before any
        patch lands. ``asyncio.wait_for`` cancels the in-flight ``complete`` call
        on expiry; the resulting ``TimeoutError`` propagates and is handled by the
        workflow/agent wrapper as a dead step (the run continues with whatever is
        already in the working tree). ``None`` disables the ceiling.
        """
        if self._per_call_timeout is None:
            return await self.llm.complete(**kwargs)
        return await asyncio.wait_for(
            self.llm.complete(**kwargs), timeout=self._per_call_timeout
        )

    async def _stop_on_context_overflow(self) -> None:
        """Graceful terminal stop when a prompt overflows the model window even
        after forced compaction + one retry. Mirrors the BUDGET_EXCEEDED
        degradation: a visible system message, an error event, and a validated
        transition to the dedicated CONTEXT_OVERFLOW terminal. A child stopped
        this way delivers a controlled result to its parent (DONE row) rather
        than crashing the parent's turn.
        """
        reason = "context overflow: prompt exceeds the model context window even after compaction"
        self.state.append_message(
            {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
        )
        await self.event_publisher.emit(self.event_factory.error("context_overflow"))
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.CONTEXT_OVERFLOW, reason=reason)

    def _maybe_trace_steering(self, level: str | None) -> None:
        """Emit a ``steering_nudge`` trace step on an UPWARD level crossing.

        ``reads_since_last_edit`` can jump past 8/16 in one batch, so the high-
        water mark (``_last_steering_level``), not equality, decides whether this
        is a new escalation. A genuine write reset re-arms so a later re-escalation
        traces again. The mark advances even when no tracer is wired, so the next
        escalation still computes correctly.
        """
        rank = {None: 0, "soft": 1, "hard": 2}
        if level is None:
            # ``level is None`` means no active write nudge this turn. Re-arm the
            # high-water mark only when a write reset actually dropped reads below
            # the soft rung; if reads is still high (e.g. a read-only session that
            # never escalates), the escalation has NOT de-escalated, so leave the
            # mark intact (re-arming would let a later still-high turn re-fire a
            # duplicate steering_nudge).
            if self.state.reads_since_last_edit < READS_NUDGE_SOFT:
                self._last_steering_level = None
            return
        if rank[level] > rank[self._last_steering_level] and self.tracer is not None:
            self.tracer.log_step(
                step_type="steering_nudge",
                payload={
                    "aid": self.state.aid,
                    "agent": getattr(self.agent, "role", None)
                    or getattr(self.agent, "label", None)
                    or self.agent.model,
                    "reads_since_last_edit": self.state.reads_since_last_edit,
                    "level": level,
                    "tool_choice_override": level == "hard",
                    "step": self.state.step_count,
                },
            )
        self._last_steering_level = level  # update high-water mark even with no tracer

    def record_llm_trace(self, response: CompletionResponse, latency: float) -> None:
        if self.tracer:
            tool_calls_log = None
            if response.tool_calls:
                tool_calls_log = [
                    {
                        "id": tc.get("id"),
                        "name": tc.get("function", {}).get("name"),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    }
                    for tc in response.tool_calls
                ]
            payload = {
                "model": self.agent.model,
                "finish_reason": response.finish_reason,
                "content": response.content,
                "tool_calls": tool_calls_log,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cache_read_tokens": getattr(response.usage, "cache_read_tokens", 0),
                    "cache_creation_tokens": getattr(response.usage, "cache_creation_tokens", 0),
                    "uncached_input_tokens": max(
                        response.usage.input_tokens
                        - getattr(response.usage, "cache_read_tokens", 0)
                        - getattr(response.usage, "cache_creation_tokens", 0),
                        0,
                    ),
                    "estimated": response.usage.estimated,
                },
            }
            raw_usage = getattr(response.usage, "raw_usage", None)
            if raw_usage:
                payload["usage"]["raw_usage"] = raw_usage
            # Record provider chain-of-thought to the trajectory when present
            # (omitted otherwise, so non-thinking traces keep their prior shape).
            reasoning = getattr(response, "reasoning", None)
            if reasoning:
                payload["reasoning"] = reasoning
            self.tracer.log_step(
                step_type="llm_call",
                payload=payload,
                tokens=response.usage.total_tokens,
                latency=latency,
            )

    def append_assistant_message(self, response: CompletionResponse) -> None:
        # An empty-stop turn (no content, no tool calls) would append a bare
        # ``{"role": "assistant"}`` message that some providers reject on the
        # next request. Skip it — handle_pending_response decides retry-vs-DONE.
        if not response.content and not response.tool_calls:
            return
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = response.tool_calls
        self.state.append_message(assistant_msg)

    async def finish_step(self, latency: float) -> None:
        # AutoSaveSubscriber listens for step_end on the bus.
        await self.event_publisher.emit(
            self.event_factory.step_end(self.state.step_count, latency)
        )
