"""Provider usage validation shared by session completion paths."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

_native_usage_owner: ContextVar[object | None] = ContextVar("native_usage_owner", default=None)
_native_usage_observer: ContextVar[Callable[[Any, BaseException | None, object | None], None] | None] = ContextVar(
    "native_usage_observer", default=None,
)


@contextmanager
def _own_native_usage() -> Iterator[None]:
    token = _native_usage_owner.set(object())
    try:
        yield
    finally:
        _native_usage_owner.reset(token)


def _observe_native_usage(usage: Any, error: BaseException | None, transport_timing: object | None) -> None:
    observer = _native_usage_observer.get()
    if observer is not None:
        observer(usage, error, transport_timing)


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


def _same_token_counts(native: Any, usage: Any) -> bool:
    return type(native) is type(usage) and all(
        getattr(native, name) == getattr(usage, name)
        for name in ("input_tokens", "output_tokens", "total_tokens")
    )


def _same_usage_measurements(native: Any, usage: Any) -> bool:
    return _same_token_counts(native, usage) and all(
        getattr(native, name) == getattr(usage, name)
        for name in ("cache_read_tokens", "cache_creation_tokens", "reasoning_tokens")
    )


@dataclass
class _ProviderUsageObservation:
    usage: Any
    error: BaseException | None
    transport_timing: object | None
    reported_usage: Any


class _CompletionUsageAccounting:
    def __init__(self, runner: Any, *, protected_call: bool) -> None:
        self.runner = runner
        self.protected_call = protected_call
        self.tokens = 0
        self._attempt_tokens: dict[object | None, int] = {}
        self._callback_usages: dict[object, Any] = {}
        self._native_usages: list[_ProviderUsageObservation] = []
        self._settled: list[tuple[Any, int]] = []
        self._reported = False

    @contextmanager
    def observe_provider_responses(self) -> Iterator[None]:
        token = _native_usage_observer.set(self._observe_provider_response)
        try:
            yield
        finally:
            _native_usage_observer.reset(token)

    def record_attempt(self, usage: Any) -> None:
        owner = _native_usage_owner.get()
        tokens = _normalize_completion_usage(usage)[1]
        self._charge(tokens)
        self._attempt_tokens[owner] = self._attempt_tokens.get(owner, 0) + tokens

    def settle_provider(self, usage: Any) -> None:
        owner = _native_usage_owner.get()
        native = self._find_native_usage(usage) if owner is None else None
        if native is not None and self._already_settled(native.reported_usage):
            return
        self._settle(usage, self._attempt_tokens.pop(owner, 0))
        if native is not None:
            native.reported_usage = usage
        elif owner is not None:
            self._callback_usages[owner] = usage

    def _observe_provider_response(
        self, usage: Any, error: BaseException | None, transport_timing: object | None,
    ) -> None:
        owner = _native_usage_owner.get()
        # A callback may settle a redacted copy of this request's usage.
        settled = self._callback_usages.pop(owner, usage)
        if settled is not usage and not _same_token_counts(usage, settled):
            settled = usage
        self._native_usages.append(_ProviderUsageObservation(usage, error, transport_timing, settled))
        # A router can forward the retry hook and keep the final callback for its chosen response.
        if owner is not None and owner in self._attempt_tokens:
            self._settle(usage, self._attempt_tokens.pop(owner))

    def _settle(self, usage: Any, attempt_tokens: int) -> None:
        tokens = _normalize_completion_usage(usage)[1]
        self._charge(tokens - attempt_tokens)
        self._settled.append((usage, tokens))

    def _already_settled(self, usage: Any) -> bool:
        return any(usage is settled for settled, _tokens in self._settled)

    def _find_native_usage(self, usage: Any) -> _ProviderUsageObservation | None:
        native = next((item for item in self._native_usages if item.usage is usage), None)
        origin = getattr(usage, "_native_usage_origin", None)
        if native is None and origin is not None:
            native = next(
                (item for item in self._native_usages
                 if getattr(item.usage, "_native_usage_origin", None) is origin
                 and _same_token_counts(item.usage, usage)),
                None,
            )
        if native is None and origin is not None:
            raw_usage = getattr(usage, "raw_usage", None)
            if raw_usage is not None:
                native = next(
                    (item for item in self._native_usages
                     if getattr(item.usage, "raw_usage", None) is raw_usage and _same_token_counts(item.usage, usage)),
                    None,
                )
        return native

    def finish(
        self, usage: Any, *, purpose: str, late: bool, error: BaseException | None = None,
        transport_timing: object | None = None,
    ) -> bool:
        if self._reported:
            return False
        # A router can retain timing from a different call, so prefer the usage payload.
        native = self._find_native_usage(usage)
        if native is None and transport_timing is not None:
            native = next(
                (item for item in self._native_usages
                 if item.transport_timing is transport_timing and _same_token_counts(item.usage, usage)),
                None,
            )
        if usage is not None and native is None:
            native = next((item for item in reversed(self._native_usages) if item.usage == usage), None)
        if native is None and getattr(usage, "_native_usage_origin", None) is not None:
            native = next(
                (item for item in reversed(self._native_usages) if _same_usage_measurements(item.usage, usage)),
                None,
            )
        # A wrapper can settle the usage it returns after removing the provider payload.
        reported_usage = usage if native is None or self._already_settled(usage) else native.reported_usage
        if usage is not None and not self._already_settled(reported_usage):
            attempt_tokens = self._attempt_tokens.pop(None, 0) if native is None else 0
            self._settle(reported_usage, attempt_tokens)
        self._reported = True
        record_usage = getattr(self.runner, "_record_usage_event", None)
        if callable(record_usage):
            for settled, tokens in self._settled:
                provider_error = next(
                    (item.error for item in self._native_usages if item.reported_usage is settled), None,
                )
                record_usage(settled, tokens, purpose=purpose, late=late,
                             error=error if settled is reported_usage and error is not None else provider_error)
        if late:
            self.record_late()
        return bool(self._settled)

    def record_late(self) -> None:
        if self._settled:
            self.runner._late_provider_usage += (self.tokens,)
            if self.runner.late_provider_usage_checkpoint is not None:
                self.runner.late_provider_usage_checkpoint()

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
        with usage_accounting.observe_provider_responses():
            return await complete(*args, **kwargs)
    except BaseException as exc:
        late = asyncio.current_task() in runner._draining_provider_tasks
        if usage_accounting.finish(getattr(exc, "usage", None), purpose=usage_purpose, late=late, error=exc):
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
