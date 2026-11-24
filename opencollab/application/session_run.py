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
    _last_assistant_answer,
    _submit_tool_choice,
    _TeamBudgetStop,
    _TokenBudgetStop,
)
from opencollab.application._session_run_usage import _CompletionUsageAccounting, _normalize_completion_usage
from opencollab.application.context_pressure import ContextPressure
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    EventPublisherPort,
    LLMPort,
    ShaperPort,
    TracePort,
)
from opencollab.application.run_control import RunControl, _RunCancelled, _SessionRunControlMixin
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.events import SessionRuntimeEvent
from opencollab.domain.session import SessionPhase, SessionState

logger = logging.getLogger(__name__)

_REQUIRED_TOOL_RETRY_NUDGE = (
    "Your previous response did not call the required tool. Call one of the "
    "currently available tools to complete this step."
)

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

class SessionRunUseCase(_SessionRunControlMixin, _SessionRunCompletionMixin):
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
        max_budget_tokens: int | None = 1_000_000,
        max_steps: int | None = 100,
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
        run_control: RunControl | None = None,
        run_id: str | None = None,
    ):
        self.agent = agent
        self._initial_agent_tools = tuple(getattr(agent, "tools", ()) or ())
        self._initial_agent_tool_choice = copy.deepcopy(
            getattr(agent, "tool_choice", None)
        )
        self.state = state
        self.llm = llm
        self._context_pressure = ContextPressure(self._estimate_context_message)
        self._response_session_id = uuid.uuid4().hex
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(
            lambda: self.state.aid
        )
        self.tool_execution = tool_execution
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.budget_reservations: list[Callable[[], int]] = []
        self._initialize_run_control(run_control, run_id)
        self.max_steps = max_steps
        self.deferrable_tool_names = deferrable_tool_names
        self.shaper = shaper
        # Inject the aggregate team ceiling without coupling this layer to Scheduler.
        # Standalone sessions use only their own cap.
        self._team_budget_exhausted = team_budget_exhausted
        # The adapter classifies provider context overflow without provider imports
        # in this layer. An unwired classifier leaves exceptions on the ERROR path.
        self._is_context_overflow = is_context_overflow or (lambda _exc: False)
        # Bound each generation because an HTTP timeout may not bound a slow
        # streaming or thinking response within the run's wall-clock budget.
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
        # High-water mark of the steering nudge level emitted so far
        # (None|'soft'|'hard'). Drives _maybe_trace_steering to log only UPWARD
        # crossings, re-arming on a write reset. Never persisted.
        self._last_steering_level: str | None = None
        # Previous steering spend drives threshold crossings. ``None`` makes
        # the first build compare spend against itself, including after restore.
        self._steering_prev_used_tokens: int | None = None
        self._session_terminal_traced = False
        # The protected submit reserve is carved from the token cap.
        # Wind-down runs only when enforcement is enabled.
        self._enforcement_strength = enforcement_strength
        self._commit_reserve = commit_reserve
        self._submit_tool_name = submit_tool_name
        # No-progress and low-yield limits share the budget wind-down actuator.
        self._watchdog_k = watchdog_k
        self._low_yield_m = low_yield_m
        self._pending_tool_allowlist: frozenset[str] | None = None
        self._pending_tool_gate_label: str | None = None
        # Record the offered tools before provider adaptation for record_llm_trace.
        self._last_request_tool_names: list[str] = []
        self._last_request_tool_choice: Any = None
        self._required_tool_retried = False
        # Message index where the current user turn began. It survives a
        # deferred suspend/resume so the returned answer is scoped to this turn.
        self._turn_start_message_index: int | None = None
        self._provider_tasks: set[asyncio.Task[Any]] = set()
        self._llm_step_started = False
        self._run_cancel_event: asyncio.Event | None = None
        self.default_cancel_event: asyncio.Event | None = None
        self._draining_provider_tasks: set[asyncio.Task[Any]] = set()
        # Successful responses returned after caller cancellation or timeout.
        # They count against the budget but never enter a later turn's history.
        self._late_provider_usage: tuple[int, ...] = ()
        # The session supplies its owned persistence queue after adopting this runner.
        self.late_provider_usage_checkpoint: Callable[[], object] | None = None
        self._on_completion_tokens: Callable[[int], None] | None = None

    def reset_runtime_for_user_turn(self) -> None:
        """Restore agent capabilities narrowed by the previous turn."""
        self.agent.tools = list(self._initial_agent_tools)
        self.agent.tool_choice = copy.deepcopy(self._initial_agent_tool_choice)
        self._pending_tool_allowlist = None
        self._pending_tool_gate_label = None
        self._required_tool_retried = False

    def reset_for_restore(self) -> None:
        """Discard process-local turn state before publishing a snapshot."""
        self.reset_runtime_for_user_turn()
        self._context_pressure.clear()
        self._pending = None
        self._empty_stop_retried = False
        self.state.submitted_summary = None
        self._last_steering_level = None
        self._steering_prev_used_tokens = None
        self._turn_start_message_index = None
        self._llm_step_started = False
        self._late_provider_usage = ()

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            set(task for task in self._provider_tasks if not task.done())
            | {task for task in self._draining_provider_tasks if not task.cancelled()}
        )

    @property
    def late_provider_usage(self) -> tuple[int, ...]:
        """Immutable token ledger for successful, abandoned provider calls."""
        return self._late_provider_usage

    def _track_provider_task(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.add(task)
        task.add_done_callback(self._provider_task_done)

    def _mark_provider_task_draining(self, task: asyncio.Task[Any]) -> None:
        self._draining_provider_tasks.add(task)

    def _provider_task_done(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.discard(task)
        self._finish_run_control_if_quiesced()
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
            and self.max_budget_tokens is not None
            and self.state.used_tokens >= self.max_budget_tokens - self._commit_reserve
        ):
            self.state.budget_reserve_consumed = True

    def _charge_completion_tokens(self, tokens: int) -> None:
        self.state.add_used_tokens(tokens)
        if tokens and self._on_completion_tokens is not None:
            self._on_completion_tokens(tokens)

    def _record_late_provider_result(
        self, task: asyncio.Future[Any], *, protected_call: bool = False,
        accounting: _CompletionUsageAccounting | None = None,
    ) -> None:
        try:
            response = task.result()
            if accounting is None:
                accounting = _CompletionUsageAccounting(self, protected_call=protected_call)
                accounting.finish(response.usage, purpose="completion", late=True)
            else:
                accounting.record_late()
        except BaseException:
            pass
        finally:
            self._draining_provider_tasks.discard(task)
            self._finish_run_control_if_quiesced()

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        """Drive the phase FSM until the turn finishes or suspends.

        Returns the last assistant text answer (empty string if none). On an
        unexpected exception the session is marked failed and the exception
        re-raised; cancellation flushes the tracer before propagating.
        """
        if self.pending_cleanup_tasks:
            raise RuntimeError("prior provider generation is still draining")
        self._run_cancel_event = cancel_event
        try:
            self._prepare_turn()
            while not self._should_suspend():
                await self.advance(cancel_event)

        except _RunCancelled:
            await self._stop_for_run_cancellation()
        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception as exc:
            self.state.fail(reason=f"{type(exc).__name__}: {exc}")
            self._emit_run_event("error", error_type=type(exc).__name__, message=str(exc))
            self._trace_session_terminal()
            raise
        finally:
            self._run_cancel_event = None

        answer = self._last_turn_answer()
        if self.is_terminal_phase():
            self.state.clear_active_turn()
            self._trace_session_terminal()
        return answer

    def _prepare_turn(self) -> None:
        """Set the answer cursor and resume the phase appropriate to this call."""
        entry_phase = self.state.phase
        if not self.state.pending_events.is_empty() and (
            entry_phase is SessionPhase.IDLE or entry_phase.is_terminal()
        ):
            self.state.resume_to_idle()
            self.state.set_phase(SessionPhase.AWAITING_EVENTS)
            entry_phase = SessionPhase.AWAITING_EVENTS
        if self.state.pending_external_user_turn is not None and entry_phase.is_terminal():
            self.state.resume_to_idle()
            entry_phase = SessionPhase.IDLE
        if entry_phase is not SessionPhase.DONE:
            self._session_terminal_traced = False
        if entry_phase is SessionPhase.IDLE:
            self.state.consume_queued_external_user_turn()
        if entry_phase is SessionPhase.AWAITING_EVENTS:
            self._turn_start_message_index = self.state.active_turn_start_message_index
        elif entry_phase is SessionPhase.DONE:
            # Re-entering an already-completed session is a compatibility
            # read-only query for its final answer, not a restored active turn.
            self._turn_start_message_index = self.state.completed_turn_start_message_index
            if self._turn_start_message_index is None:
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
            self.state.submitted_summary = None

    def _last_turn_answer(self) -> str:
        return _last_assistant_answer(self.state.messages, self._turn_start_message_index)

    def is_terminal_phase(self) -> bool:
        """Whether the session has finished its turn (done/failed/limits)."""
        return self.state.phase.is_terminal()

    def _trace_session_terminal(self) -> None:
        """Record session disposition beside token and step usage and limits.

        ``step_ceiling_reached`` distinguishes a step-limited stop from completion
        or token exhaustion. Trace construction failures leave the terminal state
        unchanged.
        """
        if getattr(self, "_run_control", None) is not None:
            self._record_run_terminal()
        if self._session_terminal_traced or self.tracer is None:
            return
        self._session_terminal_traced = True
        try:
            step_count = int(self.state.step_count)
            max_steps = None if self.max_steps is None else int(self.max_steps)
            self.tracer.log_step(
                step_type="session_terminal",
                payload={
                    "aid": self.state.aid,
                    "role": getattr(self.agent, "name", None),
                    "phase": self.state.phase.value,
                    "terminal_reason": self.state.terminal_reason,
                    "step_count": step_count,
                    "max_steps": max_steps,
                    "step_ceiling_reached": max_steps is not None and step_count >= max_steps,
                    "used_tokens": int(self.state.used_tokens),
                    "max_budget_tokens": (
                        None if self.max_budget_tokens is None else int(self.max_budget_tokens)
                    ),
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
        queued_turn = self.state.pending_external_user_turn
        if queued_turn is not None:
            self.state.consume_queued_external_user_turn()
            self._turn_start_message_index = len(self.state.messages)
            self.state.start_active_turn(self._turn_start_message_index)
            self._empty_stop_retried = False
            self.state.submitted_summary = None
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
        if (
            enforcement_strength == ENFORCEMENT_ON
            and self.max_budget_tokens is not None
            and effective_reserve > self.max_budget_tokens
        ):
            raise ValueError("commit_reserve cannot exceed max_budget_tokens")

        self._enforcement_strength = enforcement_strength
        if commit_reserve is not None:
            self._commit_reserve = commit_reserve

    def _enforcement_on(self) -> bool:
        return self._enforcement_strength != ENFORCEMENT_OFF

    def _brake_on(self) -> bool:
        """Enable watchdog and low-yield brakes under scout enforcement.

        These use information gain because read-only scouts still need progress
        brakes. ``has_write`` separately drives write nudges and integrity accounting.
        """
        return self._enforcement_on()

    def _enter_wind_down(self) -> None:
        """Allocate the protected submit turn and restrict it to the submit tool.

        Force ``tool_choice`` to avoid spending this turn on a removed exploration
        tool. Tool mutations stay within the session. If the submit tool
        is absent, retain the current tools and apply the nudge and turn limit.
        """
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
        """Choose the next phase when an enforcement brake fires.

        With enforcement enabled and pending results consumed, budget reserve,
        watchdog or low-yield exhaustion enters ``_enter_wind_down``. Durable
        attempt counts allow one protected submit turn and one compatibility retry,
        then STOPPED, including after restore. Return whether a phase was selected.
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

        budget_spent = False
        if self.max_budget_tokens is not None:
            explore_threshold = self.max_budget_tokens - self._commit_reserve
            budget_spent = self.state.used_tokens >= explore_threshold
        progress_known = not self.state.turn.last_progress_unknown
        watchdog_tripped = (
            progress_known and self._brake_on() and self.state.turn.steps_since_progress >= self._watchdog_k
        )
        low_yield_tripped = (
            progress_known and self._brake_on() and self.state.turn.low_yield_since_progress >= self._low_yield_m
        )
        if budget_spent and not watchdog_tripped and not low_yield_tripped:
            await self._decide_run_budget(reason="wind_down")
            budget_spent = (self.max_budget_tokens is not None
                            and self.state.used_tokens >= self.max_budget_tokens - self._commit_reserve)
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

    def _ensure_tool_environment_active(self) -> None:
        if getattr(self.tool_execution, "environment_revoked", False):
            raise RuntimeError("Execution environment has been revoked. Session cannot continue.")

    async def precheck(self, cancel_event: asyncio.Event | None) -> None:
        """Gate the next LLM call: cancellation, loop-block, token budget, step limit.

        Each guard appends a visible system message, emits an error event, and
        stops the session via ``_stop_precheck`` (STOPPED with a reason string);
        otherwise proceed to CALLING_LLM.
        """
        self._ensure_tool_environment_active()
        if (cancel_event and cancel_event.is_set()) or self._run_cancellation_requested():
            await self._stop_for_run_cancellation()
            return

        environment = getattr(self.tool_execution, "environment", None)
        if bool(getattr(environment, "revoked", False)):
            await self._stop_precheck("execution environment has been revoked")
            return

        if self.state.turn.loop_state.blocked_rounds >= DEFAULT_LOOP_BLOCKED_LIMIT:
            reason = f"loop block limit reached: {self.state.turn.loop_state.blocked_rounds} unproductive tool batches"
            await self._stop_precheck(reason)
            return

        try:
            await self._decide_run_budget(reason="precheck")
        except _RunCancelled:
            await self._stop_for_run_cancellation()
            return
        if (
            self.max_budget_tokens is not None
            and self.state.used_tokens >= self.max_budget_tokens
        ):
            reason = f"budget exceeded: {self.state.used_tokens} tokens used"
            await self._stop_precheck(reason)
            return

        # Stop at the aggregate team ceiling even if this session has allowance left,
        # since an overshooting generation can consume the shared pool.
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            reason = "team budget exceeded: aggregate spend reached the global cap"
            await self._stop_precheck(reason)
            return

        if await self._apply_enforcement_gate():
            return

        if self.max_steps is not None and self.state.step_count >= self.max_steps:
            reason = f"step limit reached: {self.state.step_count} steps"
            await self._stop_precheck(reason)
            return

        self.state.transition_to(SessionPhase.CALLING_LLM)

    async def run_llm_call(self) -> None:
        """Run one model call and retain its response for HANDLING_RESPONSE.

        If ``call_llm`` still overflows after forced compaction and one retry,
        catch ``_ContextOverflowStop`` and stop with CONTEXT_OVERFLOW.
        """
        self._llm_step_started = False
        self._ensure_tool_environment_active()
        start = time.monotonic()

        tools = self.build_tool_schemas()
        try:
            response = await self.call_llm(tools)
        except _RunCancelled:
            await self._stop_for_run_cancellation()
            return
        except asyncio.CancelledError as cancellation:
            try:
                self.record_llm_cancelled()
            except Exception as observation_error:
                add_note = getattr(cancellation, "add_note", None)
                if callable(add_note):
                    add_note(f"cancellation trace failed: {type(observation_error).__name__}")
            raise
        except _TokenBudgetStop as exc:
            output_shortfall = (
                f"leaving less than {exc.minimum_output_tokens} output tokens required by model configuration"
                if exc.minimum_output_tokens > 1 else "leaving no output headroom"
            )
            reason = (
                "budget exhausted before model call: conservative input reservation "
                f"requires {exc.reserved_input_tokens} of {exc.remaining_budget} "
                f"remaining tokens, {output_shortfall}"
            )
            await self._stop_precheck(reason)
            return
        except _ContextOverflowStop:
            await self._stop_on_context_overflow()
            return
        except _TeamBudgetStop:
            await self._stop_precheck("team budget exceeded: aggregate spend reached the global cap")
            return
        latency = time.monotonic() - start
        input_tokens, _total_tokens = _normalize_completion_usage(response.usage)
        self._run_final_prompt = None
        self.state.add_markup_recovered(getattr(response.usage, "markup_recovered", 0))
        self.state.set_context_tokens(input_tokens)

        self.record_llm_trace(response, latency)
        if self.max_budget_tokens is not None and self.state.used_tokens > self.max_budget_tokens:
            await self._decide_run_budget(reason="after_response")
        if (
            self.max_budget_tokens is not None
            and self.state.used_tokens > self.max_budget_tokens
        ):
            if isinstance(response.content, str) and response.content.strip():
                self.state.append_message({"role": "assistant", "content": response.content})
                await self.event_publisher.emit(self.event_factory.text_delta(response.content))
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
        # Accept a new tool batch only while the turn is still active. Keep the
        # billed response in the trace and its usable text in the transcript.
        if response.tool_calls and self._run_cancellation_requested():
            if isinstance(response.content, str) and response.content.strip():
                self.state.append_message({"role": "assistant", "content": response.content})
                await self.event_publisher.emit(self.event_factory.text_delta(response.content))
            await self.finish_step(latency)
            await self._stop_for_run_cancellation()
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

        if self._pending_tool_allowlist and response.finish_reason in (None, "stop", "end_turn"):
            if not self._required_tool_retried:
                self._required_tool_retried = True
                if self.tracer:
                    self.tracer.log_step(
                        step_type="required_tool_retry",
                        payload={"allowed_tools": sorted(self._pending_tool_allowlist)},
                        latency=pending.latency,
                    )
                if not has_content:
                    self.state.append_message({
                        "role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER,
                        "_opencollab_placeholder": True,
                    })
                self.state.append_message({"role": "user", "content": _REQUIRED_TOOL_RETRY_NUDGE})
                self.state.transition_to(SessionPhase.AUTOSAVING)
                return
            await self.finish_step(pending.latency)
            self.clear_pending_step()
            self.state.transition_to(
                SessionPhase.STOPPED, reason="required tool was not called after correction"
            )
            return

        # Retry an empty stop once per turn before declaring an answerless completion.
        # Both Chat stop and native end_turn use this route through AUTOSAVING.
        # Budget and step limits still bound the retry.
        empty_stop = not has_content and not response.tool_calls
        if empty_stop and response.finish_reason in (None, "stop", "end_turn") and not self._empty_stop_retried:
            self._empty_stop_retried = True
            if self.tracer:
                self.tracer.log_step(
                    step_type="empty_stop_retry",
                    payload={
                        "finish_reason": response.finish_reason,
                        "had_reasoning": bool(getattr(response, "reasoning", None)),
                    },
                    latency=pending.latency,
                )
            # The empty turn was omitted. Insert an assistant placeholder before the
            # nudge to avoid consecutive user messages, which Anthropic rejects.
            if self.state.messages and self.state.messages[-1]["role"] in ("user", "tool"):
                self.state.append_message({
                    "role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER,
                    "_opencollab_placeholder": True,
                })
            self.state.append_message({"role": "user", "content": _EMPTY_STOP_NUDGE})
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        await self.finish_step(pending.latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.DONE, reason="completed")
