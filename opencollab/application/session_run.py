from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
import uuid
from typing import Any, Callable

from opencollab.application._session_run_completion import _SessionRunCompletionMixin
from opencollab.application._session_run_shared import (
    _EMPTY_STOP_NUDGE,
    _EMPTY_STOP_PLACEHOLDER,
    _WIND_DOWN_NUDGE,
    _WIND_DOWN_RETRY,
    DEFAULT_COMMIT_RESERVE,
    DEFAULT_DEFERRABLE_TOOLS,
    DEFAULT_LOOP_BLOCKED_LIMIT,
    DEFAULT_LOW_YIELD_M,
    DEFAULT_WATCHDOG_K,
    ENFORCEMENT_OFF,
    ENFORCEMENT_ON,
    SUBMIT_TOOL_NAME,
    GenerationTimeoutError,
    PendingStep,
    _ContextOverflowStop,
    _submit_tool_choice,
    _TokenBudgetStop,
)
from opencollab.application._session_run_usage import _normalize_completion_usage
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    EventPublisherPort,
    LLMPort,
    ShaperPort,
    TracePort,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.events import SessionRuntimeEvent
from opencollab.domain.session import SessionPhase, SessionState

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_COMMIT_RESERVE",
    "DEFAULT_DEFERRABLE_TOOLS",
    "DEFAULT_LOOP_BLOCKED_LIMIT",
    "DEFAULT_LOW_YIELD_M",
    "DEFAULT_WATCHDOG_K",
    "ENFORCEMENT_OFF",
    "ENFORCEMENT_ON",
    "GenerationTimeoutError",
    "PendingStep",
    "SUBMIT_TOOL_NAME",
    "SessionRunUseCase",
    "_EMPTY_STOP_NUDGE",
    "_EMPTY_STOP_PLACEHOLDER",
    "_WIND_DOWN_NUDGE",
    "_WIND_DOWN_RETRY",
]


class SessionRunUseCase(_SessionRunCompletionMixin):
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
        self._initial_agent_tools = tuple(getattr(agent, "tools", ()) or ())
        self._initial_agent_tool_choice = copy.deepcopy(
            getattr(agent, "tool_choice", None)
        )
        self.state = state
        self.llm = llm
        self._response_session_id = uuid.uuid4().hex
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(
            lambda: state.aid
        )
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
                raise ValueError("per_call_timeout must be a finite positive number or None") from exc
            if not math.isfinite(normalized_per_call_timeout) or normalized_per_call_timeout <= 0:
                raise ValueError("per_call_timeout must be a finite positive number or None")
            self._per_call_timeout = normalized_per_call_timeout
        self._pending: PendingStep | None = None
        # Guards the once-per-session retry on an empty-stop turn (see
        # ``handle_pending_response``).
        self._empty_stop_retried = False
        # The summary a ``submit`` call in the step now finishing gave, or None
        # when this step did not submit. Read once by
        # ``autosave_pending_step``, which ends the turn at DONE.
        self._submitted_summary: str | None = None
        # High-water mark of the steering nudge level emitted so far
        # (None|'soft'|'hard'). Drives _maybe_trace_steering to log only UPWARD
        # crossings, re-arming on a write reset. Never persisted.
        self._last_steering_level: str | None = None
        # Spend seen the last time a steering block was built, so the
        # ``thresholds`` cadence can fire on the CROSSING of a band rather than
        # on every turn above it. ``None`` means "no previous turn in this
        # process": the first build then compares spend against itself and
        # crosses nothing, which is also what a resumed session wants.
        self._steering_prev_used_tokens: int | None = None
        # One ``session_terminal`` row per session, not per turn: ``run_loop``
        # can be re-entered on an already-finished session as a read-only query
        # for its answer, and that must not add a second disposition.
        self._session_terminal_traced = False
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
        self._pending_tool_allowlist: frozenset[str] | None = None
        self._pending_tool_gate_label: str | None = None
        # Message index where the current user turn began. It survives a
        # deferred suspend/resume so the returned answer is scoped to this turn.
        self._turn_start_message_index: int | None = None
        self._provider_tasks: set[asyncio.Task[Any]] = set()
        self._llm_step_started = False
        self._draining_provider_tasks: set[asyncio.Task[Any]] = set()
        # Successful responses that arrived only after their caller timeout.
        # They count against the budget but never enter a later turn's history.
        self._late_provider_usage: tuple[int, ...] = ()

    def reset_runtime_for_user_turn(self) -> None:
        """Restore agent capabilities narrowed by the previous turn."""
        self.agent.tools = list(self._initial_agent_tools)
        self.agent.tool_choice = copy.deepcopy(self._initial_agent_tool_choice)
        self._pending_tool_allowlist = None
        self._pending_tool_gate_label = None

    def reset_for_restore(self) -> None:
        """Discard process-local turn state before publishing a snapshot."""
        self.reset_runtime_for_user_turn()
        self._pending = None
        self._empty_stop_retried = False
        self._submitted_summary = None
        self._last_steering_level = None
        self._turn_start_message_index = None
        self._llm_step_started = False
        self._late_provider_usage = ()

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            set(task for task in self._provider_tasks if not task.done())
            | self._draining_provider_tasks
        )

    @property
    def late_provider_usage(self) -> tuple[int, ...]:
        """Immutable token ledger for successful, timed-out provider calls."""
        return self._late_provider_usage

    def _track_provider_task(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.add(task)
        task.add_done_callback(self._provider_task_done)

    def _mark_provider_task_draining(self, task: asyncio.Task[Any]) -> None:
        self._draining_provider_tasks.add(task)

    def _provider_task_done(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    def _mark_budget_reserve_consumed(self, *, protected_call: bool | None = None) -> None:
        """Keep late and ordinary protected-call accounting on one invariant."""
        if protected_call is None:
            protected_call = self.state.wind_down_done
        if (
            protected_call
            and self.state.used_tokens >= self.max_budget_tokens - self._commit_reserve
        ):
            self.state.budget_reserve_consumed = True

    def _record_late_provider_result(
        self,
        task: asyncio.Future[Any],
        *,
        protected_call: bool = False,
    ) -> None:
        """Charge a provider response that survived cancellation after timeout."""
        try:
            response = task.result()
            _input_tokens, total_tokens = _normalize_completion_usage(response.usage)
            self._late_provider_usage += (total_tokens,)
            self.state.add_used_tokens(total_tokens)
            self._mark_budget_reserve_consumed(protected_call=protected_call)
        except BaseException:
            pass
        finally:
            self._draining_provider_tasks.discard(task)

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        """Drive the phase FSM until the turn finishes or suspends.

        Returns the last assistant text answer (empty string if none). On an
        unexpected exception the session is marked failed and the exception
        re-raised; cancellation flushes the tracer before propagating.
        """
        if self.pending_cleanup_tasks:
            raise RuntimeError("prior provider generation is still draining")
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
            self._trace_session_terminal()
            raise

        answer = self._last_turn_answer()
        if self.is_terminal_phase():
            self.state.clear_active_turn()
            self._trace_session_terminal()
        return answer

    def _prepare_turn(self) -> None:
        """Set the answer cursor and resume the phase appropriate to this call."""
        entry_phase = self.state.phase
        if entry_phase is SessionPhase.IDLE:
            self.state.consume_queued_external_user_turn()
        if entry_phase is SessionPhase.AWAITING_EVENTS:
            self._turn_start_message_index = self.state.active_turn_start_message_index
        elif entry_phase is SessionPhase.DONE:
            # Re-entering an already-completed session is a compatibility
            # read-only query for its final answer, not a restored active turn.
            self._turn_start_message_index = 0
        else:
            self._turn_start_message_index = len(self.state.messages)
            self.state.start_active_turn(self._turn_start_message_index)

        if entry_phase is SessionPhase.AWAITING_EVENTS:
            self._resume_from_awaiting()
        elif entry_phase is not SessionPhase.DONE:
            self.state.resume_to_idle()
            # Deferred work belongs to the same turn; every other entry starts a
            # fresh once-per-turn empty-response retry allowance.
            self._empty_stop_retried = False
            self._submitted_summary = None

    def _last_turn_answer(self) -> str:
        """Return the last real assistant text produced by the current turn."""
        turn_start = self._turn_start_message_index
        if turn_start is None:
            return ""
        for message in reversed(self.state.messages[turn_start:]):
            content = message.get("content")
            if message["role"] == "assistant" and content and content != _EMPTY_STOP_PLACEHOLDER:
                return content
        return ""

    def is_terminal_phase(self) -> bool:
        """Whether the session has finished its turn (done/failed/limits)."""
        return self.state.phase.is_terminal()

    def _trace_session_terminal(self) -> None:
        """Record how this session ended, and what it had left when it did.

        Two resources can stop a session: the tokens it was given and the steps
        it was allowed. They cannot both be equalized across differently
        organized runs — a solo agent carries one long history and pays more per
        step than a teammate carrying a short one — so a comparison holds one of
        them equal and lets the other vary. This repository holds tokens equal.

        That makes the step ceiling a runaway guard rather than an allowance,
        and a guard is only honest if it never actually fires. Nothing recorded
        that. A session stopped at its step ceiling with tokens still unspent
        looked exactly like a session that finished: the phase collapsed to
        STOPPED, the reason string lived only in memory, and the trajectory —
        the file the run is read from afterwards — said nothing at all. The
        claim "steps were counted, never enforced" was unfalsifiable.

        So each session writes one row naming its disposition beside both
        counters and both ceilings. ``step_ceiling_reached`` is derivable from
        the two step fields and is written anyway: it is the exact question this
        record exists to answer, and a reader should not have to re-derive the
        rule to ask it.

        Observation only, and guarded: a record that cannot be built must not
        change how the session ended.
        """
        if self._session_terminal_traced or self.tracer is None:
            return
        self._session_terminal_traced = True
        try:
            step_count = int(self.state.step_count)
            max_steps = int(self.max_steps)
            self.tracer.log_step(
                step_type="session_terminal",
                payload={
                    "aid": self.state.aid,
                    "role": getattr(self.agent, "name", None),
                    "phase": self.state.phase.value,
                    "terminal_reason": self.state.terminal_reason,
                    "step_count": step_count,
                    "max_steps": max_steps,
                    "step_ceiling_reached": step_count >= max_steps,
                    "used_tokens": int(self.state.used_tokens),
                    "max_budget_tokens": int(self.max_budget_tokens),
                },
            )
        except Exception as exc:  # noqa: BLE001 — observability is non-authoritative
            logger.error("session terminal trace failed: %s", exc)

    def _should_suspend(self) -> bool:
        """The loop stops on a terminal phase (turn finished) OR on the
        non-terminal AWAITING_EVENTS suspend state (turn paused on deferred
        work; the scheduler re-activates it when the events arrive).
        """
        return self.is_terminal_phase() or self.state.phase is SessionPhase.AWAITING_EVENTS

    def _resume_from_awaiting(self) -> None:
        """Drain a complete pending table back into the message history as one
        contiguous tool-result block, then finish its suspended step through
        AUTOSAVING. Defensive no-op if woken while still incomplete (the loop
        guard then stops at once).
        """
        table = self.state.pending_events
        if not table.is_complete():
            return
        for message in table.ordered_results():
            self.state.append_message(message)
        table.clear()
        self.state.transition_to(SessionPhase.AUTOSAVING)

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
            raise ValueError(f"enforcement_strength must be {ENFORCEMENT_OFF!r} or {ENFORCEMENT_ON!r}")
        effective_reserve = self._commit_reserve if commit_reserve is None else commit_reserve
        if isinstance(effective_reserve, bool) or not isinstance(effective_reserve, int) or effective_reserve <= 0:
            raise ValueError("commit_reserve must be a positive integer")
        if enforcement_strength == ENFORCEMENT_ON and effective_reserve > self.max_budget_tokens:
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
        self.state.wind_down_attempts += 1
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

        Once wound down, the durable attempt count grants EXACTLY ONE protected
        commit turn and a single retry if that turn strays, then a terminal
        STOPPED — never an unbounded forced loop, including after restore.
        """
        if not self._enforcement_on():
            return False
        if self.state.wind_down_done:
            if self.state.wind_down_attempts < 2:
                self.state.wind_down_attempts += 1
                self.state.append_message({"role": "system", "content": _WIND_DOWN_RETRY})
                self.state.transition_to(SessionPhase.CALLING_LLM)
                return True
            await self._stop_precheck("wind-down complete: forced commit within reserve")
            return True

        explore_threshold = self.max_budget_tokens - self._commit_reserve
        budget_spent = self.state.used_tokens >= explore_threshold
        watchdog_tripped = self._brake_on() and self.state.turn.steps_since_progress >= self._watchdog_k
        low_yield_tripped = self._brake_on() and self.state.turn.low_yield_since_progress >= self._low_yield_m
        brake = budget_spent or watchdog_tripped or low_yield_tripped
        if not brake or not self.state.pending_events.is_empty():
            return False
        if budget_spent and self.state.budget_reserve_consumed:
            await self._stop_precheck(
                "budget reserve exhausted: protected commit turn already used"
            )
            return True

        self._trace_brake_trip(budget_spent, watchdog_tripped, low_yield_tripped)
        if budget_spent:
            self.state.budget_reserve_consumed = True
            await self.event_publisher.emit(
                SessionRuntimeEvent(
                    type="budget_reserve_allocated",
                    data={
                        "aid": self.state.aid,
                        "used_tokens": self.state.used_tokens,
                        "reserve": self._commit_reserve,
                    },
                )
            )
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
            reason = f"loop block limit reached: {self.state.turn.loop_blocked_since_progress} repeated tool calls"
            await self._stop_precheck(reason)
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

        if await self._apply_enforcement_gate():
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
        self._llm_step_started = False
        start = time.monotonic()

        tools = self.build_tool_schemas()
        try:
            response = await self.call_llm(tools)
        except _TokenBudgetStop as exc:
            reason = (
                "budget exhausted before model call: conservative input reservation "
                f"requires {exc.reserved_input_tokens} of {exc.remaining_budget} "
                "remaining tokens, leaving no output headroom"
            )
            await self._stop_precheck(reason)
            return
        except _ContextOverflowStop:
            await self._stop_on_context_overflow()
            return
        latency = time.monotonic() - start
        input_tokens, total_tokens = _normalize_completion_usage(response.usage)
        self.state.add_used_tokens(total_tokens)
        self._mark_budget_reserve_consumed()
        self.state.add_markup_recovered(getattr(response.usage, "markup_recovered", 0))
        self.state.set_context_tokens(input_tokens)

        self.record_llm_trace(response, latency)
        if self.state.used_tokens > self.max_budget_tokens:
            reason = (
                "budget exceeded after model call: "
                f"{self.state.used_tokens} tokens used"
            )
            self.state.append_message(
                {
                    "role": "system",
                    "content": f"[{reason.capitalize()}. Session stopped.]",
                }
            )
            await self.event_publisher.emit(self.event_factory.error(reason))
            await self.finish_step(latency)
            self.state.transition_to(SessionPhase.STOPPED, reason=reason)
            return
        self.append_assistant_message(response)
        self._pending = PendingStep(
            response=response,
            latency=latency,
            started_at=start,
        )
        self.state.transition_to(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        """Route the pending LLM response: tool calls → EXECUTING_TOOLS;
        a plain text answer finishes the turn (DONE)."""
        pending = self._pending
        if pending is None:
            self.state.fail()
            raise RuntimeError("Cannot handle assistant response before calling LLM")
        response = pending.response

        has_content = (
            isinstance(response.content, str)
            and bool(response.content.strip())
        )
        if has_content:
            await self.event_publisher.emit(self.event_factory.text_delta(response.content))

        if response.finish_reason in {
            "length",
            "max_tokens",
            "model_context_window_exceeded",
        }:
            reason = "output truncated: provider reached its generation limit"
            self.state.append_message(
                {
                    "role": "system",
                    "content": "[Output truncated by the provider. Partial response preserved; session stopped.]",
                }
            )
            await self.event_publisher.emit(self.event_factory.error(reason))
            await self.finish_step(pending.latency)
            self.clear_pending_step()
            self.state.transition_to(SessionPhase.STOPPED, reason=reason)
            return

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
        empty_stop = not has_content and not response.tool_calls
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
                self.state.append_message({"role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER})
            self.state.append_message({"role": "user", "content": _EMPTY_STOP_NUDGE})
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        await self.finish_step(pending.latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.DONE, reason="completed")
