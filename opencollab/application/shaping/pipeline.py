"""Shaper pipeline, window-derived trigger math, and group-span helpers."""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Any, Iterator

from opencollab.application.ports import ShaperPort
from opencollab.domain.token_estimation import estimate_request_message_tokens

# History-compaction thresholds (token estimates over the whole view). The
# trigger/target gap is deliberate: a layer compacts down to ``TARGET`` (well
# below ``TRIGGER``) so the next turn does not immediately re-trigger — this is
# the anti-thrash headroom (cf. Liu et al. 2026 §4.4 reactive-compact).
DEFAULT_HISTORY_TRIGGER_TOKENS = 120_000
DEFAULT_HISTORY_TARGET_TOKENS = 90_000
# Most-recent groups always kept verbatim (never snipped or summarized).
DEFAULT_HISTORY_KEEP_RECENT_GROUPS = 4

# Context-source priority at/above which a message is *pinned*: never cleared,
# snipped, or summarized. Sources are tagged with their resolved layer priority
# (``domain.context.LAYER_PRIORITY``) via the ``_ctx`` key; identity/team/task
# sit above this floor, project/memory below it. Untagged messages (tool work,
# turns) have no ``_ctx`` and are governed by the recency-based layers instead.
PIN_FLOOR = 70

# Reported by ``ShaperPipeline.shape_with_report`` when a whole pass left the
# view untouched. Emitting it (rather than nothing) is what lets a reader tell
# "no rung fired this turn" apart from "this turn was never recorded".
_NO_RUNG_FIRED = "none"
# A shaper wired into the chain without a ``rung`` label. Unreachable with the
# five real rungs (each declares its own); kept so an unlabeled shaper is
# reported as an obvious wiring bug instead of silently masquerading as "none".
_UNLABELED_RUNG = "unknown"

# Window-derived trigger math. The effective input budget reserves room for the
# next output; the trigger also keeps a safety buffer. Compaction targets a
# lower ratio so the next turn does not immediately compact again.
DEFAULT_OUTPUT_RESERVE_TOKENS = 20_000  # held back for the summary/answer response
DEFAULT_COMPACT_BUFFER_TOKENS = 13_000  # safety margin below the effective window
HISTORY_TARGET_RATIO = 0.75


def require_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def history_trigger_target(
    context_window: int | None,
    *,
    output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    buffer: int = DEFAULT_COMPACT_BUFFER_TOKENS,
    target_ratio: float = HISTORY_TARGET_RATIO,
) -> tuple[int, int]:
    """Derive ``(trigger, target)`` token thresholds from the model's real window.

    Scales the reactive history layers to the active model instead of fixed
    constants. Degrades to the fixed ``DEFAULT_HISTORY_*`` defaults when the
    window is unknown (``None``/non-positive), so an unrecognised model still
    gets sane behaviour.
    """
    require_nonnegative_int(output_reserve, "output_reserve")
    require_nonnegative_int(buffer, "buffer")
    if (
        isinstance(target_ratio, bool)
        or not isinstance(target_ratio, (int, float))
        or not math.isfinite(target_ratio)
        or not 0 < target_ratio < 1
    ):
        raise ValueError("target_ratio must be finite and satisfy 0 < target_ratio < 1")
    if context_window is not None and (
        isinstance(context_window, bool) or not isinstance(context_window, int)
    ):
        raise ValueError("context_window must be an integer or None")
    if not context_window or context_window <= 0:
        return DEFAULT_HISTORY_TRIGGER_TOKENS, DEFAULT_HISTORY_TARGET_TOKENS
    trigger = max(2, context_window - output_reserve - buffer)
    target = max(1, min(trigger - 1, int(trigger * target_ratio)))
    return trigger, target


def approx_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap, additive context-size estimate over a message list.

    Additive per message so subtracting a group's estimate equals the estimate
    of the remainder — lets the history layers re-estimate incrementally. A
    real tokenizer-backed estimator is injected at wiring time; this fallback
    uses the same serialized request fields as budget accounting, provider
    state included, so a thinking-heavy history is not read as shorter here
    than the budget check reads it. The once-per-request framing allowance is
    excluded because a constant term would break additivity.

    Deliberately keeps ``keep_reasoning_content`` at its default (True), unlike
    the pre-call budget reservation, which drops recorded reasoning when
    streaming strips it from the outbound request. This is a compaction
    threshold, not a reservation: the recorded reasoning still occupies the
    local history that compaction is sizing, and shrinking the estimate here
    would move every arm's compaction trigger. See ``test_shaping_pipeline``.
    """
    return estimate_request_message_tokens(messages)


@contextmanager
def _forced_layers(shaper: ShaperPort) -> Iterator[None]:
    """Temporarily flip every reactive history layer reachable from ``shaper``
    into forced mode (compact unconditionally toward target), restoring the
    prior flags on exit. A shaper without a ``_forced`` flag is left untouched
    (e.g. the per-tool-result cap, which is already unconditional). Recurses
    into a nested ``ShaperPipeline`` so a wrapped chain is covered too.
    """
    toggled: list[Any] = []
    stack: list[ShaperPort] = [shaper]
    while stack:
        node = stack.pop()
        if isinstance(node, ShaperPipeline):
            stack.extend(node._shapers)
            continue
        if hasattr(node, "_forced"):
            toggled.append(node)
    saved = [node._forced for node in toggled]
    for node in toggled:
        node._forced = True
    try:
        yield
    finally:
        for node, prior in zip(toggled, saved):
            node._forced = prior


def forced_shape(
    shaper: ShaperPort, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run ``shaper`` in forced maximal-compaction mode.

    The safety-net entry point used after a real context-overflow rejection:
    every reactive history layer compacts unconditionally toward its target
    instead of no-op'ing below the char estimate (which provably under-counted,
    since the provider rejected the prompt). Pinned sources (identity/team/task
    at/above ``PIN_FLOOR``) are still never folded — so when the pinned seed
    alone overflows the window, this is a no-op and the caller falls through to
    the graceful-stop path.
    """
    with _forced_layers(shaper):
        return shaper.shape(messages)


def _rung_label(shaper: ShaperPort) -> str:
    """The frozen trajectory label a shaper declares for itself."""
    label = getattr(shaper, "rung", None)
    return label if isinstance(label, str) and label else _UNLABELED_RUNG


class ShaperPipeline:
    """An ordered chain of shapers applied left-to-right.

    An empty pipeline is the identity transform. Each shaper receives the
    output of the previous one; the original ``messages`` is never mutated.
    """

    def __init__(self, shapers: tuple[ShaperPort, ...] = ()):
        self._shapers = tuple(shapers)

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = messages
        for shaper in self._shapers:
            result = shaper.shape(result)
        return result

    def shape_with_report(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``shape``, plus a per-rung account of what actually fired.

        Returns ``(shaped, reports)``; each report is ``{"rung",
        "tokens_before", "tokens_after"}`` with ``rung`` the label the shaper
        declares for itself (never its class name). A rung counts as fired when
        its output differs in CONTENT from its input — deliberately not "the
        token estimate moved", since two different message lists can estimate
        to the same number. When nothing fires the result is a single
        ``none`` report, so a caller persisting these can tell an untouched
        turn from an unrecorded one.

        Reporting only: this writes nowhere. The caller owns the sink, which
        keeps this package a pure transform with no I/O of its own.
        """
        result = messages
        reports: list[dict[str, Any]] = []
        tokens = approx_messages_tokens(result)
        for shaper in self._shapers:
            result, entries = self._run_rung(shaper, result, tokens)
            if entries:
                reports.extend(entries)
                tokens = entries[-1]["tokens_after"]
        if not reports:
            reports.append(
                {
                    "rung": _NO_RUNG_FIRED,
                    "tokens_before": tokens,
                    "tokens_after": tokens,
                }
            )
        return result, reports

    def _run_rung(
        self,
        shaper: ShaperPort,
        messages: list[dict[str, Any]],
        tokens_before: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Apply one chain entry; return its output and the reports it earned.

        A nested pipeline reports its own inner rungs (its ``none`` filler is
        dropped — the outer pass decides whether the whole turn was untouched).
        An entry that changed nothing earns no report.
        """
        if isinstance(shaper, ShaperPipeline):
            result, nested = shaper.shape_with_report(messages)
            if result == messages:
                return result, []
            return result, [e for e in nested if e["rung"] != _NO_RUNG_FIRED]
        result = shaper.shape(messages)
        if result == messages:
            return result, []
        return result, [
            {
                "rung": _rung_label(shaper),
                "tokens_before": tokens_before,
                "tokens_after": approx_messages_tokens(result),
            }
        ]


def _group_spans(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Partition messages into ``(start, end)`` spans that are safe to drop or
    replace as a unit.

    An assistant message carrying ``tool_calls`` is grouped with the ``tool``
    messages that answer it, so removing/summarizing a whole group never
    orphans a ``tool_call_id`` (the provider requires every call to be
    answered). Every other message is its own singleton group.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(messages)
    while i < n:
        start = i
        leader = messages[i]
        i += 1
        if leader.get("role") == "assistant" and leader.get("tool_calls"):
            while i < n and messages[i].get("role") == "tool":
                i += 1
        spans.append((start, i))
    return spans


def matched_tool_result_occurrences(
    messages: list[dict[str, Any]],
) -> list[tuple[int, str, str, Any]]:
    """Return unambiguous tool-result occurrences in assistant issue order.

    Each tuple is ``(result_index, call_id, tool_name, arguments)``. Pairing is
    local to one assistant turn, so providers that reuse an id in later turns
    cannot cause an old compaction decision to affect the newest result. A
    malformed group (missing, unknown, or duplicate ids) is skipped entirely.
    """
    matched: list[tuple[int, str, str, Any]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        calls = list(message.get("tool_calls") or ())
        call_ids = [call.get("id") for call in calls]
        if (
            any(not isinstance(call_id, str) or not call_id for call_id in call_ids)
            or len(set(call_ids)) != len(call_ids)
        ):
            continue

        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        results = list(enumerate(messages[index + 1 : end], start=index + 1))
        result_ids = [result.get("tool_call_id") for _, result in results]
        if (
            any(not isinstance(result_id, str) or not result_id for result_id in result_ids)
            or len(set(result_ids)) != len(result_ids)
            or set(result_ids) != set(call_ids)
        ):
            continue
        result_index = {
            result.get("tool_call_id"): result_i for result_i, result in results
        }
        for call in calls:
            function = call.get("function") or {}
            matched.append(
                (
                    result_index[call["id"]],
                    call["id"],
                    function.get("name"),
                    function.get("arguments"),
                )
            )
    return matched


def is_complete_tool_exchange(
    messages: list[dict[str, Any]], span: tuple[int, int]
) -> bool:
    """Whether a tool-leading span has a one-to-one local call/result pairing."""
    start, end = span
    leader = messages[start]
    if leader.get("role") != "assistant" or not leader.get("tool_calls"):
        return False
    call_ids = [call.get("id") for call in leader.get("tool_calls") or ()]
    result_ids = [
        messages[index].get("tool_call_id")
        for index in range(start + 1, end)
        if messages[index].get("role") == "tool"
    ]
    return bool(call_ids) and (
        all(isinstance(call_id, str) and call_id for call_id in call_ids)
        and all(isinstance(result_id, str) and result_id for result_id in result_ids)
        and len(call_ids) == len(set(call_ids))
        and len(result_ids) == len(set(result_ids))
        and set(call_ids) == set(result_ids)
    )


def span_is_safe_to_compact(
    messages: list[dict[str, Any]], span: tuple[int, int]
) -> bool:
    """Reject malformed protocol fragments from deletion or summarization."""
    start, _end = span
    leader = messages[start]
    if leader.get("role") == "tool":
        return False
    if leader.get("role") == "assistant" and leader.get("tool_calls"):
        return is_complete_tool_exchange(messages, span)
    return True


def _droppable_region(
    messages: list[dict[str, Any]], keep_recent_groups: int
) -> tuple[list[tuple[int, int]], int, int]:
    """Group spans plus the ``[lo, hi)`` group range eligible for compaction.

    The leading group (group 0 — the system prompt / first turn) and the last
    ``keep_recent_groups`` groups are always protected; the open middle is what
    the history layers may touch. ``lo >= hi`` means nothing is eligible.
    """
    spans = _group_spans(messages)
    lo = 1  # protect the leading (system) group
    hi = max(lo, len(spans) - keep_recent_groups)
    return spans, lo, hi


def ctx_priority(message: dict[str, Any]) -> int | None:
    """The source priority stamped on a message, or ``None`` if untagged."""
    ctx = message.get("_ctx")
    return ctx.get("priority") if isinstance(ctx, dict) else None


def is_pinned(message: dict[str, Any]) -> bool:
    """True if the message is a context source at/above ``PIN_FLOOR``."""
    priority = ctx_priority(message)
    return priority is not None and priority >= PIN_FLOOR


def pinned_free_region(
    messages: list[dict[str, Any]],
    spans: list[tuple[int, int]],
    lo: int,
    hi: int,
) -> tuple[int, int]:
    """Narrow ``[lo, hi)`` to a contiguous run of groups holding no pinned message.

    A region-collapsing layer (auto-compact) must not fold a pinned source
    (identity/team/task) into a summary. Pinned seed messages sit at the head of
    the droppable region, so this advances ``lo`` past any leading pinned group
    and stops ``hi`` at the first pinned group after it — yielding the largest
    pinned-free prefix run. ``lo >= hi`` means nothing summarizable remains.
    """
    def group_has_pinned(span: tuple[int, int]) -> bool:
        start, end = span
        return any(is_pinned(messages[i]) for i in range(start, end))

    while lo < hi and group_has_pinned(spans[lo]):
        lo += 1
    end = lo
    while end < hi and not group_has_pinned(spans[end]):
        end += 1
    return lo, end
