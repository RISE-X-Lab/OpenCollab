from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    CompletionResponse,
    EventPublisherPort,
    LLMPort,
    ShaperPort,
    TracePort,
)
from opencollab.application.shaping import forced_shape
from opencollab.application.steering import (
    READS_NUDGE_SOFT,
    build_steering_block,
    fold_steering,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP
from opencollab.domain.pending import PendingEventTable, PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import ToolProcessingResult

logger = logging.getLogger(__name__)

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})


class GenerationTimeoutError(asyncio.TimeoutError):
    """A single provider generation exceeded its per-call ceiling."""


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

# Closed-loop steering (the reads-without-write nudge) lives in
# ``application/steering.py``; the tool vocabulary it keys on stays here because
# wind-down shares it. ``READS_NUDGE_SOFT`` is imported above for the trace seam's
# re-arm check.
_READ_TOOLS = frozenset({"file_read", "grep"})
_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
_STRUCTURED_OUTPUT_TOOL = "structured_output"

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
DEFAULT_LOOP_BLOCKED_LIMIT = 3

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
        if per_call_timeout is None:
            self._per_call_timeout = None
        else:
            if isinstance(per_call_timeout, bool):
                raise ValueError("per_call_timeout must be a finite positive number or None")
            try:
                normalized_per_call_timeout = float(per_call_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "per_call_timeout must be a finite positive number or None"
                ) from exc
            if (
                not math.isfinite(normalized_per_call_timeout)
                or normalized_per_call_timeout <= 0
            ):
                raise ValueError(
                    "per_call_timeout must be a finite positive number or None"
                )
            self._per_call_timeout = normalized_per_call_timeout
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
        # Guards the once-per-session provider-compat retry on the wind-down turn
        # (a model that ignored the forced tool_choice and called another/unknown
        # tool gets exactly ONE more turn before going terminal).
        self._wind_down_retried = False
        self._pending_tool_allowlist: frozenset[str] | None = None
        self._pending_tool_gate_label: str | None = None
        # Message index where the current user turn began. It survives a
        # deferred suspend/resume so the returned answer is scoped to this turn.
        self._turn_start_message_index: int | None = None
        self._provider_tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(task for task in self._provider_tasks if not task.done())

    def _track_provider_task(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.add(task)
        task.add_done_callback(self._provider_task_done)

    def _provider_task_done(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        """Drive the phase FSM until the turn finishes or suspends.

        Returns the last assistant text answer (empty string if none). On an
        unexpected exception the session is marked failed and the exception
        re-raised; cancellation flushes the tracer before propagating.
        """
        try:
            self._prepare_turn()
            while not self._should_suspend():
                await self.advance(cancel_event)

        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception as exc:
            self.state.fail(reason=f"{type(exc).__name__}: {exc}")
            raise

        return self._last_turn_answer()

    def _prepare_turn(self) -> None:
        """Set the answer cursor and resume the phase appropriate to this call."""
        entry_phase = self.state.phase
        if entry_phase in {SessionPhase.AWAITING_EVENTS, SessionPhase.DONE}:
            if self._turn_start_message_index is None:
                # Restored sessions do not yet persist this runtime-only cursor.
                self._turn_start_message_index = 0
        else:
            self._turn_start_message_index = len(self.state.messages)

        if entry_phase is SessionPhase.AWAITING_EVENTS:
            self._resume_from_awaiting()
        elif entry_phase is not SessionPhase.DONE:
            self.state.resume_to_idle()
            # Deferred work belongs to the same turn; every other entry starts a
            # fresh once-per-turn empty-response retry allowance.
            self._empty_stop_retried = False

    def _last_turn_answer(self) -> str:
        """Return the last real assistant text produced by the current turn."""
        turn_start = self._turn_start_message_index or 0
        for message in reversed(self.state.messages[turn_start:]):
            content = message.get("content")
            if (
                message["role"] == "assistant"
                and content
                and content != _EMPTY_STOP_PLACEHOLDER
            ):
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
    ) -> None:
        """Turn the enforcement wind-down on/off post-construction.

        The workflow engine calls this on the scout's session (the agent already
        carries the submit tool) instead of threading the flag through every
        factory layer. ``commit_reserve`` is carved FROM the cap, never added.
        """
        if enforcement_strength not in {ENFORCEMENT_OFF, ENFORCEMENT_ON}:
            raise ValueError(
                f"enforcement_strength must be {ENFORCEMENT_OFF!r} or {ENFORCEMENT_ON!r}"
            )
        effective_reserve = (
            self._commit_reserve if commit_reserve is None else commit_reserve
        )
        if (
            isinstance(effective_reserve, bool)
            or not isinstance(effective_reserve, int)
            or effective_reserve <= 0
        ):
            raise ValueError("commit_reserve must be a positive integer")
        if (
            enforcement_strength == ENFORCEMENT_ON
            and effective_reserve > self.max_budget_tokens
        ):
            raise ValueError("commit_reserve cannot exceed max_budget_tokens")

        self._enforcement_strength = enforcement_strength
        if commit_reserve is not None:
            self._commit_reserve = commit_reserve

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
        watchdog_tripped: bool,
        low_yield_tripped: bool,
    ) -> None:
        """Trace which trigger routed the scout into the forced-commit actuator
        (STEP 3). No-op without a tracer; only reachable with enforcement on, so the
        reference (off) path is untouched. ``trigger`` is the highest-signal cause so
        the STEP-6 control-health scorecard can attribute spin-brakes (watchdog /
        low-yield) vs the budget threshold."""
        if self.tracer is None:
            return
        if budget_spent:
            trigger = "budget"
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
                "watchdog_tripped": watchdog_tripped,
                "low_yield_tripped": low_yield_tripped,
                "steps_since_progress": self.state.turn.steps_since_progress,
                "low_yield_since_progress": self.state.turn.low_yield_since_progress,
                "used_tokens": self.state.used_tokens,
                "step": self.state.step_count,
            },
        )

    async def _stop_precheck(
        self,
        reason: str,
        *,
        message: str | None = None,
    ) -> None:
        """Record one precheck rejection and stop the session (STOPPED).

        ``reason`` is the single disposition detail: it becomes ``terminal_reason``,
        the emitted error-event reason, and (unless ``message`` overrides) the
        visible system message. There is one graceful-stop terminal; the *why*
        lives in this string, not in a parallel phase/event taxonomy.
        """
        content = message or f"[{reason.capitalize()}. Session stopped.]"
        self.state.append_message({"role": "system", "content": content})
        await self.event_publisher.emit(self.event_factory.error(reason))
        self.state.transition_to(SessionPhase.STOPPED, reason=reason)

    async def _apply_enforcement_gate(self) -> bool:
        """The enforcement funnel: THREE triggers -> ONE actuator. Returns whether
        it chose the next phase (so the caller yields the transition to it).

        Off unless enforcement is on (the self-regulating default never enters).
        When on, three independent brakes converge on a single actuator
        (``_enter_wind_down``, which narrows the toolset to submit-only and forces
        the commit): the token budget spent past the commit reserve, the progress
        watchdog (``steps_since_progress`` >= K), and the low-yield sensor
        (``low_yield_since_progress`` >= M). Any one trips it; none fires while a
        tool result is still un-ingested (``pending_events`` non-empty).

        Once wound down, the latch grants EXACTLY ONE protected commit turn, then a
        single retry (``_wind_down_retried``) if that turn strays, then a terminal
        STOPPED — never an unbounded forced loop.
        """
        if not self._enforcement_on():
            return False
        if self.state.wind_down_done:
            if not self._wind_down_retried:
                self._wind_down_retried = True
                self.state.append_message({"role": "system", "content": _WIND_DOWN_RETRY})
                self.state.transition_to(SessionPhase.CALLING_LLM)
                return True
            await self._stop_precheck(
                "wind-down complete: forced commit within reserve"
            )
            return True

        explore_threshold = self.max_budget_tokens - self._commit_reserve
        budget_spent = self.state.used_tokens >= explore_threshold
        watchdog_tripped = (
            self._brake_on() and self.state.turn.steps_since_progress >= self._watchdog_k
        )
        low_yield_tripped = (
            self._brake_on()
            and self.state.turn.low_yield_since_progress >= self._low_yield_m
        )
        brake = budget_spent or watchdog_tripped or low_yield_tripped
        if not brake or not self.state.pending_events.is_empty():
            return False

        self._trace_brake_trip(budget_spent, watchdog_tripped, low_yield_tripped)
        self._enter_wind_down()
        self.state.transition_to(SessionPhase.CALLING_LLM)
        return True

    async def precheck(self, cancel_event: asyncio.Event | None) -> None:
        """Gate the next LLM call: cancellation, loop-block, token budget, step limit.

        Each guard appends a visible system message, emits an error event, and
        stops the session via ``_stop_precheck`` (STOPPED with a reason string);
        otherwise proceed to CALLING_LLM.
        """
        if cancel_event and cancel_event.is_set():
            await self._stop_precheck(
                "interrupted by user",
                message="[Session interrupted by user]",
            )
            return

        if self.state.turn.loop_blocked_since_progress >= DEFAULT_LOOP_BLOCKED_LIMIT:
            reason = (
                "loop block limit reached: "
                f"{self.state.turn.loop_blocked_since_progress} repeated tool calls"
            )
            await self._stop_precheck(reason)
            return

        if await self._apply_enforcement_gate():
            return

        if self.state.used_tokens >= self.max_budget_tokens:
            reason = f"budget exceeded: {self.state.used_tokens} tokens used"
            await self._stop_precheck(reason)
            return

        # Aggregate ceiling (defense-in-depth): even when this session is under
        # its own cap, stop if the *team total* has reached the global cap. A
        # single overshooting turn or fan-out could otherwise spend past the
        # global pool that reserve-at-allocation is meant to protect.
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            reason = "team budget exceeded: aggregate spend reached the global cap"
            await self._stop_precheck(reason)
            return

        if self.state.step_count >= self.max_steps:
            reason = f"step limit reached: {self.state.step_count} steps"
            await self._stop_precheck(reason)
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

        original_tool_calls = list(pending.response.tool_calls)
        tool_calls, blocked_messages = self._apply_pending_tool_allowlist(original_tool_calls)
        immediate, deferred = self._split_tool_calls(tool_calls)

        # Fast path, unchanged: every tool is synchronous — run them, append
        # results, and autosave the step.
        if not deferred:
            result = (
                await self.tool_execution.process(tool_calls)
                if tool_calls
                else ToolProcessingResult()
            )
            result.messages_to_append = self._ordered_tool_messages(
                original_tool_calls,
                result.messages_to_append,
                blocked_messages,
            )
            result.apply_to(self.state)
            self._pending_tool_allowlist = None
            self._pending_tool_gate_label = None
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        # A deferred tool is present. Buffer the WHOLE batch into the pending
        # table so the eventual tool-result block stays contiguous and in the
        # original order, satisfying the LLM rule that every tool_call_id is
        # answered before the next model call.
        table = self.state.pending_events
        order = {tc["id"]: i for i, tc in enumerate(original_tool_calls)}
        completed_messages = list(blocked_messages)
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
            completed_messages = [*proc.messages_to_append, *completed_messages]
        self._buffer_completed_rows(table, order, completed_messages)
        await self._execute_deferred_tools(table, order, deferred)

        self._pending_tool_allowlist = None
        self._pending_tool_gate_label = None
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

    @staticmethod
    def _buffer_completed_rows(
        table: PendingEventTable,
        order: dict[str, int],
        messages: list[dict],
    ) -> None:
        for message in messages:
            tool_call_id = message["tool_call_id"]
            table.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.IMMEDIATE,
                    order=order[tool_call_id],
                    status=RowStatus.DONE,
                    result=message["content"],
                )
            )

    async def _execute_deferred_tools(
        self,
        table: PendingEventTable,
        order: dict[str, int],
        tool_calls: list[dict],
    ) -> None:
        for tool_call in tool_calls:
            tool_call_id = tool_call["id"]
            # Register before the child starts so an immediate completion has a
            # row to fill while execute_deferred is still emitting its event.
            table.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.CHILD_AGENT,
                    order=order[tool_call_id],
                    status=RowStatus.PENDING,
                )
            )
            ref, error = await self.tool_execution.execute_deferred(tool_call)
            if ref is not None:
                table.rows[tool_call_id] = replace(table.rows[tool_call_id], ref=ref)
            else:
                table.fill(
                    tool_call_id,
                    status=RowStatus.FAILED,
                    result=error or "Deferred tool failed without an error message.",
                    error=error,
                )

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

    def _write_only_tool_schemas(self, tools: list[dict] | None) -> list[dict] | None:
        return self._tool_schemas_with_names(tools, _WRITE_TOOLS)

    def _tool_schemas_with_names(
        self, tools: list[dict] | None, names: frozenset[str]
    ) -> list[dict] | None:
        if not tools:
            return tools
        filtered = [
            spec
            for spec in tools
            if spec.get("function", {}).get("name") in names
        ]
        return filtered or tools

    def _hard_steering_tools(self) -> tuple[frozenset[str], str]:
        tool_names = {
            getattr(t, "name", None)
            for t in getattr(self.agent, "tools", []) or []
        }
        if tool_names & _WRITE_TOOLS:
            return _WRITE_TOOLS, "hard write gate"
        if _STRUCTURED_OUTPUT_TOOL in tool_names:
            return frozenset({_STRUCTURED_OUTPUT_TOOL}), "hard structured-output gate"
        return frozenset(), "hard tool gate"

    def _apply_pending_tool_allowlist(
        self,
        tool_calls: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        allowed = self._pending_tool_allowlist
        if allowed is None:
            return tool_calls, []

        kept: list[dict] = []
        blocked: list[dict] = []
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name")
            if name in allowed:
                kept.append(tc)
                continue
            allowed_list = ", ".join(sorted(allowed))
            gate_label = self._pending_tool_gate_label or "hard tool gate"
            blocked.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": (
                        f"Error: tool '{name}' is not allowed during the "
                        f"{gate_label}. Use one of: {allowed_list}."
                    ),
                }
            )
        return kept, blocked

    def _ordered_tool_messages(
        self,
        tool_calls: list[dict],
        *message_groups: list[dict],
    ) -> list[dict]:
        by_id: dict[str, dict] = {}
        extras: list[dict] = []
        for group in message_groups:
            for message in group:
                tool_call_id = message.get("tool_call_id")
                if isinstance(tool_call_id, str):
                    by_id[tool_call_id] = message
                else:
                    extras.append(message)
        ordered = [
            by_id[tc["id"]]
            for tc in tool_calls
            if tc.get("id") in by_id
        ]
        return ordered + extras

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
        tool_names = {
            getattr(t, "name", None)
            for t in getattr(self.agent, "tools", []) or []
        }
        steering, tool_choice_override, steering_level = build_steering_block(
            used_tokens=self.state.used_tokens,
            max_budget_tokens=self.max_budget_tokens or 0,
            step_count=self.state.step_count,
            max_steps=self.max_steps,
            reads=self.state.turn.reads_since_last_edit,
            has_write=bool(tool_names & _WRITE_TOOLS),
            has_structured_output=_STRUCTURED_OUTPUT_TOOL in tool_names,
            structured_override=_submit_tool_choice(_STRUCTURED_OUTPUT_TOOL),
        )
        self._maybe_trace_steering(steering_level)
        persisted = (
            steering is not None
            and bool(self.state.messages)
            and self.state.messages[-1].get("role") == "user"
        )
        if persisted:
            self.state.messages[-1] = fold_steering(
                self.state.messages[-1], steering["content"]
            )
        messages = (
            self.shaper.shape(self.state.messages)
            if self.shaper is not None
            else self.state.messages
        )
        if steering is not None and not persisted:
            messages = [*messages, steering]
        if steering_level == "hard":
            hard_tools, gate_label = self._hard_steering_tools()
            self._pending_tool_allowlist = hard_tools
            self._pending_tool_gate_label = gate_label
            tools = self._tool_schemas_with_names(tools, hard_tools)
        else:
            self._pending_tool_allowlist = None
            self._pending_tool_gate_label = None

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
        tool_choice_override: Any | None = None,
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
        self, messages: list[dict], tools: list[dict] | None, tool_choice: Any | None
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
        max_output_tokens = getattr(
            self.agent, "max_tokens_per_step", DEFAULT_MAX_TOKENS_PER_STEP
        )
        if max_output_tokens != DEFAULT_MAX_TOKENS_PER_STEP:
            extra["max_output_tokens"] = max_output_tokens
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
        try:
            return await abandon_on_timeout(
                self.llm.complete(**kwargs),
                self._per_call_timeout,
                task_tracker=self._track_provider_task,
            )
        except CallerTimeoutError as exc:
            raise GenerationTimeoutError(
                f"LLM generation exceeded the {self._per_call_timeout}s per-call timeout"
            ) from exc

    async def _stop_on_context_overflow(self) -> None:
        """Graceful terminal stop when a prompt overflows the model window even
        after forced compaction + one retry. Mirrors the budget-exhaustion
        degradation: a visible system message, an error event, and a validated
        transition to STOPPED (reason names the overflow). A child stopped this
        way delivers a controlled result to its parent (DONE row) rather than
        crashing the parent's turn.
        """
        reason = "context overflow: prompt exceeds the model context window even after compaction"
        self.state.append_message(
            {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
        )
        await self.event_publisher.emit(self.event_factory.error(reason))
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.STOPPED, reason=reason)

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
            if self.state.turn.reads_since_last_edit < READS_NUDGE_SOFT:
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
                    "reads_since_last_edit": self.state.turn.reads_since_last_edit,
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
            usage = response.usage
            input_tokens = getattr(usage, "input_tokens", 0)
            total_tokens = getattr(usage, "total_tokens", input_tokens)
            payload = {
                "model": self.agent.model,
                "finish_reason": response.finish_reason,
                "content": response.content,
                "tool_calls": tool_calls_log,
            }
            if usage is not None:
                output_tokens = getattr(usage, "output_tokens", max(total_tokens - input_tokens, 0))
                cache_read_tokens = getattr(usage, "cache_read_tokens", 0)
                cache_creation_tokens = getattr(usage, "cache_creation_tokens", 0)
                payload["usage"] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "uncached_input_tokens": max(
                        input_tokens
                        - cache_read_tokens
                        - cache_creation_tokens,
                        0,
                    ),
                    "estimated": getattr(usage, "estimated", False),
                }
                raw_usage = getattr(usage, "raw_usage", None)
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
                tokens=total_tokens,
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
