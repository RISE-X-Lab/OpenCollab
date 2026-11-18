"""Provider usage validation shared by session completion paths."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


def _nonnegative_usage_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"provider usage {field} must be a non-negative integer")
    return value

def _normalize_completion_usage(usage: Any) -> tuple[int, int]:
    """Validate provider counters atomically and prevent total undercharging."""
    input_tokens = _nonnegative_usage_int(
        getattr(usage, "input_tokens", None),
        "input_tokens",
    )
    reported_total = _nonnegative_usage_int(
        getattr(usage, "total_tokens", None),
        "total_tokens",
    )
    raw_output = getattr(usage, "output_tokens", None)
    output_tokens = (
        max(0, reported_total - input_tokens)
        if raw_output is None
        else _nonnegative_usage_int(raw_output, "output_tokens")
    )
    context_tokens = getattr(usage, "context_tokens", None)
    if context_tokens is not None:
        context_tokens = _nonnegative_usage_int(context_tokens, "context_tokens")
    return input_tokens if context_tokens is None else context_tokens, max(reported_total, input_tokens + output_tokens)


class _CompletionUsageAccounting:
    def __init__(self, runner: Any, *, protected_call: bool) -> None:
        self.runner = runner
        self.protected_call = protected_call
        self.tokens = 0

    def record_attempt(self, usage: Any) -> None:
        self._charge(_normalize_completion_usage(usage)[1])

    def settle(self, usage: Any) -> None:
        self._charge(_normalize_completion_usage(usage)[1] - self.tokens)

    def _charge(self, tokens: int) -> None:
        self.runner._charge_completion_tokens(tokens)
        self.tokens += tokens
        self.runner._mark_budget_reserve_consumed(protected_call=self.protected_call)


async def _complete_with_error_usage(
    runner: Any,
    complete: Callable[..., Awaitable[Any]],
    *args: Any,
    usage_accounting: _CompletionUsageAccounting,
    usage_purpose: str = "completion",
    **kwargs: Any,
) -> Any:
    """Charge reported error usage inside the provider owner exactly once."""
    start = time.monotonic()
    try:
        return await complete(*args, **kwargs)
    except BaseException as exc:
        usage = getattr(exc, "usage", None)
        if usage is not None:
            _input_tokens, total_tokens = _normalize_completion_usage(usage)
            usage_accounting.settle(usage)
            late = asyncio.current_task() in runner._draining_provider_tasks
            record_usage = getattr(runner, "_record_usage_event", None)
            if callable(record_usage):
                record_usage(usage, total_tokens, purpose=usage_purpose,
                             late=late, error=exc)
            if late:
                runner._late_provider_usage += (total_tokens,)
                if runner.late_provider_usage_checkpoint is not None:
                    runner.late_provider_usage_checkpoint()
            try:
                runner.record_llm_trace(
                    None, time.monotonic() - start, purpose=usage_purpose,
                    thinking=kwargs.get("thinking"), error=exc, late=late,
                )
            except Exception as observation_error:
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(f"error usage trace failed: {type(observation_error).__name__}")
        raise
