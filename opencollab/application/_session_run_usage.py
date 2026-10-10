"""Provider usage validation shared by session completion paths."""

from __future__ import annotations

import asyncio
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


async def _complete_with_error_usage(
    runner: Any,
    complete: Callable[..., Awaitable[Any]],
    *args: Any,
    protected_call: bool,
    usage_purpose: str = "completion",
    **kwargs: Any,
) -> Any:
    """Charge reported error usage inside the provider owner exactly once."""
    try:
        return await complete(*args, **kwargs)
    except BaseException as exc:
        usage = getattr(exc, "usage", None)
        if usage is not None:
            _input_tokens, total_tokens = _normalize_completion_usage(usage)
            runner.state.add_used_tokens(total_tokens)
            record_usage = getattr(runner, "_record_usage_event", None)
            if callable(record_usage):
                record_usage(usage, total_tokens, purpose=usage_purpose,
                             late=asyncio.current_task() in runner._draining_provider_tasks, error=exc)
            runner._mark_budget_reserve_consumed(protected_call=protected_call)
            if asyncio.current_task() in runner._draining_provider_tasks:
                runner._late_provider_usage += (total_tokens,)
                if runner.late_provider_usage_checkpoint is not None:
                    runner.late_provider_usage_checkpoint()
        raise
