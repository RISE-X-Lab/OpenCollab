"""Provider usage validation shared by session completion paths."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import unwrap
from typing import Any, Awaitable, Callable

_native_usage_owner: ContextVar[object | None] = ContextVar("native_usage_owner", default=None)
_native_usage_observer: ContextVar[
    Callable[[Any, BaseException | None, object | None, Callable[[Any], None] | None], None] | None
] = ContextVar(
    "native_usage_observer", default=None,
)


@contextmanager
def _own_native_usage() -> Iterator[_UsageOrigin | None]:
    origin = _UsageOrigin()
    token = _native_usage_owner.set(origin)
    try:
        yield origin if _native_usage_observer.get() is not None else None
    finally:
        _native_usage_owner.reset(token)


def _observe_native_usage(
    usage: Any, error: BaseException | None, response: Any, on_attempt_usage: Callable[[Any], None] | None,
) -> None:
    observer = _native_usage_observer.get()
    if observer is not None:
        observer(usage, error, response, on_attempt_usage)


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


def _same_token_counts(first: Any, second: Any) -> bool:
    return all(
        getattr(first, name, None) == getattr(second, name, None)
        for name in ("input_tokens", "output_tokens", "total_tokens")
    )


class _UsageOrigin:
    __slots__ = ()

    def __deepcopy__(self, _memo: dict[int, Any]) -> _UsageOrigin:
        return self


def _tag_usage(usage: Any, origin: object) -> None:
    try:
        if hasattr(type(usage), "_native_usage_origin") and getattr(usage, "_native_usage_origin", None) is None:
            usage._native_usage_origin = origin
    except (AttributeError, TypeError):
        pass


def _same_usage_origin(first: Any, second: Any) -> bool:
    origin = getattr(first, "_native_usage_origin", None)
    return first is second or (origin is not None and origin is getattr(second, "_native_usage_origin", None))


def _completion_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _completion_json_value(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_completion_json_value(item) for item in value]
    return value


def _same_serialized_usage(first: Any, second: Any) -> bool:
    def fields(usage: Any) -> dict[str, Any]:
        values = _completion_json_value(getattr(usage, "__dict__", {}))
        # A Usage reconstructed from a missing cache counter defaults to zero.
        for name in ("cache_read_tokens", "cache_creation_tokens"):
            if values.get(name) == 0:
                values.pop(name)
        return values

    first_fields = fields(first)
    return bool(first_fields) and first_fields == fields(second)


@dataclass(eq=False)
class _UsageSettlement:
    usage: Any
    tokens: int
    charged: bool
    error: BaseException | None = None


@dataclass
class _ProviderUsageObservation:
    usage: Any
    response: Any
    provider_items: object | None
    transport_timing: object | None
    error: BaseException | None
    settlement: _UsageSettlement | None
    task: asyncio.Task[Any] | None
    uses_session_retry_accounting: bool
    prepaid_tokens: int | None = None


@dataclass
class _RoutedUsageCallback:
    settlement: _UsageSettlement
    boundary: int
    observed: int
    predecessor: _ProviderUsageObservation | None


class _CompletionUsageAccounting:
    def __init__(self, runner: Any, *, protected_call: bool) -> None:
        self.runner = runner
        self.protected_call = protected_call
        self.tokens = 0
        self._attempt_tokens: dict[object | None, int] = {}
        self._callback_usages: dict[object, _UsageSettlement] = {}
        self._native_usages: list[_ProviderUsageObservation] = []
        self._settlements: list[_UsageSettlement] = []
        self._routed_callbacks: list[_RoutedUsageCallback] = []
        self._routed_boundaries: dict[asyncio.Task[Any] | None, int] = {}
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
        if owner is not None:
            self._callback_usages[owner] = self._settle(usage, self._attempt_tokens.pop(owner, 0))
            return
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        boundary = self._routed_boundaries.get(task, 0)
        observed = len(self._native_usages)
        self._routed_boundaries[task] = observed
        native = self._find_native_usage(usage)
        if native is not None and _same_token_counts(native.usage, usage):
            if native.settlement is None:
                native.settlement = self._settle(usage, native.prepaid_tokens or 0, error=native.error)
            else:
                native.settlement.usage = usage
            _tag_usage(usage, _UsageOrigin())
            return

        # Reconstructed callbacks may report an aggregate already charged during native retry.
        # Resolve those against the returned response before incrementing the budget again.
        attempt_tokens = self._attempt_tokens.pop(None, 0)
        possible_duplicate = native is None and not attempt_tokens and any(
            (item.settlement is not None or item.prepaid_tokens is not None) and _same_token_counts(item.usage, usage)
            for item in self._native_usages
        )
        _tag_usage(usage, _UsageOrigin())
        settlement = self._settle(usage, attempt_tokens, defer=possible_duplicate)
        window = self._native_usages[boundary:observed]
        preceding = [item for item in window if task is not None and item.task is task]
        if possible_duplicate:
            unpaid = [
                item for item in window
                if item.uses_session_retry_accounting and item.settlement is None and item.prepaid_tokens is None
                and _same_token_counts(item.usage, usage)
            ]
            if len(unpaid) == 1:
                # Charge the owned request before admission revalidates another call.
                # Keep the ambiguous report separate until completion identifies its source.
                item = unpaid[0]
                item.prepaid_tokens = _normalize_completion_usage(item.usage)[1]
                self._charge(item.prepaid_tokens)
                preceding = unpaid
        predecessor = preceding[0] if len(preceding) == 1 else None
        self._routed_callbacks.append(_RoutedUsageCallback(settlement, boundary, observed, predecessor))

    def _observe_provider_response(
        self, usage: Any, error: BaseException | None, response: Any, on_attempt_usage: Callable[[Any], None] | None,
    ) -> None:
        owner = _native_usage_owner.get()
        if owner is not None:
            _tag_usage(usage, owner)
        settlement = self._callback_usages.pop(owner, None)
        if settlement is not None and _same_token_counts(usage, settlement.usage):
            if owner is not None:
                _tag_usage(settlement.usage, owner)
            settlement.error = error
        else:
            settlement = None
        if owner in self._attempt_tokens:
            settlement = self._settle(usage, self._attempt_tokens.pop(owner), error=error)
        attempt_callback = self.record_attempt
        try:
            uses_session_retry_accounting = (
                on_attempt_usage == attempt_callback or unwrap(on_attempt_usage) == attempt_callback
            )
        except ValueError:
            uses_session_retry_accounting = False
        self._native_usages.append(_ProviderUsageObservation(
            usage, response, getattr(response, "provider_items", None), getattr(response, "transport_timing", None),
            error, settlement, asyncio.current_task(), uses_session_retry_accounting,
        ))

    def _settle(self, usage: Any, attempt_tokens: int = 0, *, error: BaseException | None = None,
                defer: bool = False) -> _UsageSettlement:
        tokens = _normalize_completion_usage(usage)[1]
        if not defer:
            self._charge(tokens - attempt_tokens)
        settlement = _UsageSettlement(usage, tokens, not defer, error)
        self._settlements.append(settlement)
        return settlement

    def _find_native_usage(self, usage: Any, response: Any = None) -> _ProviderUsageObservation | None:
        for item in self._native_usages:
            if _same_usage_origin(item.usage, usage) or (
                item.settlement is not None and _same_usage_origin(item.settlement.usage, usage)
            ):
                return item
        if response is not None:
            for item in self._native_usages:
                if item.response is response:
                    return item
        raw_usage = getattr(usage, "raw_usage", None)
        if raw_usage is not None:
            matches = [item for item in self._native_usages if getattr(item.usage, "raw_usage", None) is raw_usage]
            if len(matches) == 1:
                return matches[0]
        # A dataclass replacement retains its provider items even when a router reuses another call's timing.
        provider_items = getattr(response, "provider_items", None)
        if provider_items is not None:
            matches = [item for item in self._native_usages if item.provider_items is provider_items]
            if len(matches) == 1:
                return matches[0]
        transport_timing = getattr(response, "transport_timing", None)
        if transport_timing is not None and len(self._native_usages) == 1:
            item = self._native_usages[0]
            if item.transport_timing is transport_timing:
                return item
        if not isinstance(transport_timing, dict) or transport_timing.get("request_started_at") is None:
            return None
        # JSON serializers may omit null object fields in timing and provider output.
        output_fields = (
            "content", "tool_calls", "finish_reason", "reasoning", "provider_items", "provider_model", "provider_state",
        )
        timing = _completion_json_value(transport_timing)
        output = tuple(_completion_json_value(getattr(response, field, None)) for field in output_fields)
        matches = [
            item for item in self._native_usages
            if item.response is not None
            and _completion_json_value(item.transport_timing) == timing
            and _same_token_counts(item.usage, usage)
            and tuple(_completion_json_value(getattr(item.response, field, None)) for field in output_fields) == output
        ]
        return matches[0] if len(matches) == 1 else None

    def finish(
        self, usage: Any, *, purpose: str, late: bool, error: BaseException | None = None,
        response: Any = None,
    ) -> bool:
        if self._reported:
            return False
        native = self._find_native_usage(usage, response)
        final = None
        # A returned response locates its callback across helpers. A discarded result can
        # still have an outer callback from the same task before the next native call.
        for callback in self._routed_callbacks:
            reported = callback.settlement
            returned = _same_usage_origin(reported.usage, usage) and _same_token_counts(reported.usage, usage)
            native_precedes_callback = native is not None and any(
                item is native for item in self._native_usages[:callback.observed]
            )
            if not reported.charged and any(
                item is native for item in self._native_usages[callback.boundary:callback.observed]
            ) and _same_serialized_usage(reported.usage, usage):
                returned = True
            target = callback.predecessor
            if returned:
                final = reported
                if native_precedes_callback:
                    target = native
                elif response is not None and target is not None and target.prepaid_tokens is not None:
                    target = None
            if target is None or not _same_token_counts(target.usage, reported.usage):
                continue
            if target.settlement is None:
                if target.prepaid_tokens is not None:
                    reported.charged = True
                target.settlement = reported
            elif not reported.charged:
                target.settlement.usage = reported.usage
                self._settlements.remove(reported)
                if returned:
                    final = target.settlement
            target.settlement.error = target.error

        if native is not None and _same_token_counts(native.usage, usage):
            if native.settlement is None:
                native.settlement = self._settle(native.usage, native.prepaid_tokens or 0, error=native.error)
            final = native.settlement
        elif usage is not None and final is None:
            final = self._settle(usage, self._attempt_tokens.pop(None, 0))

        for item in self._native_usages:
            if item.prepaid_tokens is not None and item.settlement is None:
                item.settlement = self._settle(item.usage, item.prepaid_tokens, error=item.error)
        self._reported = True
        record_usage = getattr(self.runner, "_record_usage_event", None)
        for settlement in self._settlements:
            if not settlement.charged:
                self._charge(settlement.tokens)
            if callable(record_usage):
                record_usage(settlement.usage, settlement.tokens, purpose=purpose, late=late,
                             error=error if settlement is final and error is not None else settlement.error)
        if late:
            self.record_late()
        return bool(self._settlements)

    def record_late(self) -> None:
        if self._settlements:
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
