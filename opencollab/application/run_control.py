"""Per-run budget decisions and compact observations for programmatic hosts."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    run_id: str | None
    session_id: str
    aid: int
    used_tokens: int
    steps: int
    soft_budget_tokens: int | None
    hard_budget_tokens: int | None
    reserved_input_tokens: int
    minimum_output_tokens: int
    reason: str


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    soft_budget_tokens: int | None
    final_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class RunEvent:
    type: str
    run_id: str | None
    session_id: str
    aid: int
    used_tokens: int
    steps: int
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunControl:
    """Configuration for each session's owned runtime.

    Decisions may await host work. Event receivers run synchronously at the
    accounting point, including after cancellation while providers settle.
    """

    initial_soft_budget_tokens: int | None = None
    decide_budget: Callable[[BudgetSnapshot], BudgetDecision | Awaitable[BudgetDecision]] | None = None
    on_event: Callable[[RunEvent], None] | None = None
    history_trigger_tokens: int | None = None
    tool_cancellation_cleanup_timeout: float | None = None

    def __post_init__(self) -> None:
        for name in ("initial_soft_budget_tokens", "history_trigger_tokens"):
            value = getattr(self, name)
            minimum = 2 if name == "history_trigger_tokens" else 1
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
                raise ValueError(f"{name} must be an integer of at least {minimum} or None")
        timeout = self.tool_cancellation_cleanup_timeout
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("tool_cancellation_cleanup_timeout must be a finite positive number or None")
        for name in ("decide_budget", "on_event"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")


class _SessionRunControlMixin:
    def _initialize_run_control(self, control: RunControl | None, run_id: str | None) -> None:
        if control is not None and not isinstance(control, RunControl):
            raise TypeError("run_control must be a RunControl or None")
        self._run_control = control
        self._run_event_receiver = None if control is None else control.on_event
        self._run_id = run_id
        self.hard_budget_tokens = self.max_budget_tokens
        self._run_observation_errors: list[str] = []
        self._run_terminal_emitted = False
        self._run_cleanup_requested = False
        self._run_cleanup_emitted = False
        self._run_final_prompt: str | None = None
        if control is not None and control.initial_soft_budget_tokens is not None:
            self.max_budget_tokens = (
                control.initial_soft_budget_tokens if self.hard_budget_tokens is None
                else min(control.initial_soft_budget_tokens, self.hard_budget_tokens)
            )

    @property
    def observation_errors(self) -> tuple[str, ...]:
        return tuple(self._run_observation_errors)

    def _emit_run_event(self, event_type: str, **data: Any) -> None:
        receiver = self._run_event_receiver
        if receiver is None:
            return
        event = RunEvent(
            type=event_type, run_id=self._run_id, session_id=self._response_session_id,
            aid=self.state.aid, used_tokens=self.state.used_tokens, steps=self.state.step_count,
            data=copy.deepcopy(data),
        )
        try:
            result = receiver(event)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("on_event must return synchronously")
        except (Exception, asyncio.CancelledError) as exc:
            self._run_observation_errors.append(f"{event_type}: {type(exc).__name__}: {exc}")
            logger.error("run event receiver failed: %s", exc)

    def _observe_runtime_event(self, event: Any) -> None:
        if getattr(event, "type", None) == "error":
            self._emit_run_event("error", **event.data)

    def _record_usage_event(self, usage: Any, total_tokens: int, *, purpose: str, late: bool = False,
                            error: BaseException | None = None) -> None:
        self._emit_run_event(
            "usage", total_tokens=total_tokens, input_tokens=usage.input_tokens,
            output_tokens=total_tokens - usage.input_tokens, purpose=purpose, late=late,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0),
            cache_creation_tokens=getattr(usage, "cache_creation_tokens", 0),
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
            estimated=getattr(usage, "estimated", False),
            error_type=None if error is None else type(error).__name__,
        )

    async def _decide_run_budget(self, *, reason: str, reserved_input_tokens: int = 0,
                                 minimum_output_tokens: int = 1) -> None:
        control = self._run_control
        if control is None or control.decide_budget is None:
            return
        if self.hard_budget_tokens is not None and self.state.used_tokens >= self.hard_budget_tokens:
            return
        snapshot = BudgetSnapshot(
            run_id=self._run_id, session_id=self._response_session_id, aid=self.state.aid,
            used_tokens=self.state.used_tokens, steps=self.state.step_count,
            soft_budget_tokens=self.max_budget_tokens, hard_budget_tokens=self.hard_budget_tokens,
            reserved_input_tokens=reserved_input_tokens, minimum_output_tokens=minimum_output_tokens,
            reason=reason,
        )
        decision: object = None
        try:
            decision = control.decide_budget(snapshot)
            if inspect.isawaitable(decision):
                decision = await decision
            if not isinstance(decision, BudgetDecision):
                raise ValueError("budget callback must return BudgetDecision")
            proposed = decision.soft_budget_tokens
            if proposed is None:
                if self.hard_budget_tokens is not None or self.max_budget_tokens is not None:
                    raise ValueError("a finite allowance requires a finite suggestion")
            else:
                if isinstance(proposed, bool) or not isinstance(proposed, int) or proposed < 1:
                    raise ValueError("suggested soft budget must be a positive integer")
                if self.max_budget_tokens is None or proposed < self.max_budget_tokens:
                    raise ValueError("suggested soft budget cannot decrease the current allowance")
                if proposed != self.max_budget_tokens and proposed < self.state.used_tokens:
                    raise ValueError("an increased soft budget cannot be below used tokens")
                if self.hard_budget_tokens is not None and proposed > self.hard_budget_tokens:
                    raise ValueError("suggested soft budget exceeds the hard allowance")
            if decision.final_prompt is not None and (
                not isinstance(decision.final_prompt, str) or not decision.final_prompt.strip()
                or "\x00" in decision.final_prompt
            ):
                raise ValueError("final_prompt must be non-empty text or None")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit_run_event("budget_decision", reason=reason, accepted=False,
                                 suggested_budget=getattr(decision, "soft_budget_tokens", None),
                                 error=str(exc))
            raise ValueError(f"run-control budget policy failed: {exc}") from exc
        self.max_budget_tokens = decision.soft_budget_tokens
        if decision.final_prompt is not None:
            self._run_final_prompt = decision.final_prompt
        self._emit_run_event("budget_decision", reason=reason, accepted=True,
                             soft_budget_tokens=self.max_budget_tokens,
                             hard_budget_tokens=self.hard_budget_tokens)

    def _record_run_terminal(self) -> None:
        if self._run_control is None or self._run_terminal_emitted or not self.state.phase.is_terminal():
            return
        self._run_terminal_emitted = True
        self._emit_run_event("session_stopped", phase=self.state.phase.value,
                             reason=self.state.terminal_reason,
                             pending_provider_requests=len(self.pending_cleanup_tasks))

    def finish_run_control(self) -> None:
        """Release this run's receiver after its existing cleanup owners settle."""
        self._run_cleanup_requested = True
        self._finish_run_control_if_quiesced()

    def _finish_run_control_if_quiesced(self) -> None:
        if not self._run_cleanup_requested or self._run_cleanup_emitted or self.pending_cleanup_tasks:
            return
        self._run_cleanup_emitted = True
        self._record_run_terminal()
        self._emit_run_event("cleanup_completed", pending_provider_requests=0)
        self._run_event_receiver = None
        self._run_control = None

