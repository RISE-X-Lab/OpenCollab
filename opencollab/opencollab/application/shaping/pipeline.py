"""Shaper pipeline, window-derived trigger math, and group-span helpers."""

from __future__ import annotations

from typing import Any

from opencollab.application.ports import ShaperPort

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

# Window-derived trigger math (ref: context-compaction-py effective_context_window).
# effective = context_window - output_reserve; trigger = effective - buffer; the
# layers then compact down to ``trigger * HISTORY_TARGET_RATIO`` (anti-thrash).
DEFAULT_OUTPUT_RESERVE_TOKENS = 20_000  # held back for the summary/answer response
DEFAULT_COMPACT_BUFFER_TOKENS = 13_000  # safety margin below the effective window
HISTORY_TARGET_RATIO = 0.75


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
    if not context_window or context_window <= 0:
        return DEFAULT_HISTORY_TRIGGER_TOKENS, DEFAULT_HISTORY_TARGET_TOKENS
    trigger = max(1, context_window - output_reserve - buffer)
    target = max(1, int(trigger * target_ratio))
    return trigger, target


def approx_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap, additive context-size estimate (~chars/4) over a message list.

    Additive per message so subtracting a group's estimate equals the estimate
    of the remainder — lets the history layers re-estimate incrementally. A
    real tokenizer-backed estimator is injected at wiring time; this is the
    dependency-free fallback (the spec only needs an approximation).
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or ():
            total += len(str(call.get("function", {}).get("arguments", "")))
    return total // 4


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
