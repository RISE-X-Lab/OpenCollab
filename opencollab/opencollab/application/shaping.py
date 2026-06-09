"""Context shapers — deterministic message reshaping before each model call.

A shaper is the *certain* counterpart to prompt guidance: prompts steer the
model probabilistically ("read files in narrow ranges"), while a shaper
guarantees an outcome regardless of whether the model complied. They run in
``SessionRunUseCase.call_llm`` against a copy of the history, so the persisted
transcript keeps the full, auditable content while the model sees a bounded
view.

The pipeline is an ordered, **lazy-degradation** chain (Liu et al. 2026,
§3.1/4.3): cheaper, lower-loss layers run before costlier ones, and each layer
only acts under the pressure it is responsible for. Two orthogonal pressures:

* **Per-message explosion** — ``PerToolResultBudgetShaper`` caps *any one* tool
  result. It is unconditional: every oversize result is bounded each call.
* **History accumulation** — after dozens of turns the *total* view approaches
  the context limit even when no single message is oversize. The history layers
  below are **reactive**: they no-op until estimated context crosses a trigger,
  then degrade progressively —

    A. ``OldHistorySnipShaper``  — cheapest. Drops whole old, low-reference
       tool-exchange turns (pure deletion, no model call).
    B. ``AutoCompactShaper``     — last resort. Summarizes the remaining old
       span into one *visible* marker via an injected summarizer (model call;
       default-off switch).
    C. ``ContextCollapseShaper`` — reserved read-time-projection slot; identity
       this period (insertion point only).

Every history layer is a read-time projection over a *copy*: ``state.messages``
and the persisted transcript always keep the full original history, so a resume
rebuilds losslessly from the transcript. Layers run oldest/cheapest-first; each
re-estimates the (already-reshaped) input it receives, so once a cheaper layer
brings the view under the trigger the costlier ones see no pressure and no-op.
"""

from __future__ import annotations

from typing import Any, Callable

from opencollab.application.ports import ShaperPort, TokenEstimatorPort

# Per-tool-result character budget. A single tool result larger than this is
# truncated (for the model's view only) to its head plus a re-read pointer.
DEFAULT_TOOL_RESULT_BUDGET = 16_000

# History-compaction thresholds (token estimates over the whole view). The
# trigger/target gap is deliberate: a layer compacts down to ``TARGET`` (well
# below ``TRIGGER``) so the next turn does not immediately re-trigger — this is
# the anti-thrash headroom (cf. Liu et al. 2026 §4.4 reactive-compact).
DEFAULT_HISTORY_TRIGGER_TOKENS = 120_000
DEFAULT_HISTORY_TARGET_TOKENS = 90_000
# Most-recent groups always kept verbatim (never snipped or summarized).
DEFAULT_HISTORY_KEEP_RECENT_GROUPS = 4

# Tool-output clearing (ToolOutputClearShaper). Old results from these bulky,
# reconstructible read-only tools have their *content* replaced in place; the
# last ``KEEP_RECENT`` compactable results stay verbatim. Names are real
# OpenCollab tool names (see ``bootstrap/container.py``), not a hardcoded set.
DEFAULT_CLEARED_TOOL_CONTENT = "[Old tool result content cleared]"
DEFAULT_TOOL_CLEAR_KEEP_RECENT = 5
DEFAULT_COMPACTABLE_TOOLS = frozenset(
    {"bash", "file_read", "grep", "git_diff", "run_tests"}
)

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

# Visible marker prefix for an auto-compacted segment — the summary announces
# itself as a compressed stand-in rather than masquerading as original history
# (Liu et al. 2026 §11.3: no invisible compression).
COMPACTED_MARKER_PREFIX = "[Context auto-compacted"

# A synchronous summarizer: given a contiguous message segment, return summary
# prose. Kept sync because ``ShaperPort.shape`` is sync; ``None`` disables the
# auto-compact layer (the default-off switch).
SummarizerPort = Callable[[list[dict[str, Any]]], str]


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


class PerToolResultBudgetShaper:
    """Caps each tool-result message at ``max_chars`` for the model's view.

    A tool message whose ``content`` exceeds the budget is replaced (in a new
    dict — the input list and its messages are left untouched) with its head
    slice plus a reference notice telling the model how to recover the rest by
    re-reading a narrower range. The result is guaranteed to fit the budget.
    """

    def __init__(self, max_chars: int = DEFAULT_TOOL_RESULT_BUDGET):
        self.max_chars = max_chars

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shaped: list[dict[str, Any]] = []
        for message in messages:
            shaped.append(self._shape_message(message))
        return shaped

    def _shape_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("role") != "tool":
            return message
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= self.max_chars:
            return message
        # Reserve headroom so head + notice still fits the budget. The notice is
        # short and bounded; a fixed 200-char reserve covers it comfortably.
        head_len = max(0, self.max_chars - 200)
        dropped = len(content) - head_len
        notice = (
            f"\n\n[truncated {dropped} chars — re-read a narrower range "
            f"(file_read with offset/limit, or grep) to see the rest]"
        )
        return {**message, "content": content[:head_len] + notice}


# ---------------------------------------------------------------------------
# History-compaction layers (reactive, lazy-degradation)
# ---------------------------------------------------------------------------


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


class _ReactiveHistoryShaper:
    """Shared trigger/estimate plumbing for the reactive history layers."""

    def __init__(
        self,
        *,
        estimate_tokens: TokenEstimatorPort = approx_messages_tokens,
        trigger_tokens: int = DEFAULT_HISTORY_TRIGGER_TOKENS,
        target_tokens: int = DEFAULT_HISTORY_TARGET_TOKENS,
        keep_recent_groups: int = DEFAULT_HISTORY_KEEP_RECENT_GROUPS,
    ):
        self._estimate = estimate_tokens
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
        self.keep_recent_groups = keep_recent_groups

    def _over_trigger(self, messages: list[dict[str, Any]]) -> bool:
        return bool(messages) and self._estimate(messages) > self.trigger_tokens


class ToolOutputClearShaper(_ReactiveHistoryShaper):
    """Layer A0 — lowest-loss history compaction: clear old tool *content*.

    Less lossy than ``OldHistorySnipShaper``: instead of deleting whole
    tool-exchange turns, it keeps the call/answer skeleton (and the assistant's
    reasoning) intact and replaces only the bulky *content* of OLD compactable
    tool results with a short placeholder. The most recent ``keep_recent``
    compactable results stay verbatim. No model call; zero orphan risk (the tool
    message survives — only its content shrinks). Reactive: identity below the
    trigger. Idempotent: an already-cleared result is skipped. Slotted before
    ``OldHistorySnipShaper`` so the cheaper/lower-loss layer runs first.

    Only ``compactable_tools`` results (large, reconstructible read-only outputs)
    are cleared; edits/writes and coordination tool results are left untouched.
    The dropped content survives in ``state.messages`` / the transcript.
    """

    def __init__(
        self,
        *,
        compactable_tools: frozenset[str] = DEFAULT_COMPACTABLE_TOOLS,
        cleared_content: str = DEFAULT_CLEARED_TOOL_CONTENT,
        keep_recent: int = DEFAULT_TOOL_CLEAR_KEEP_RECENT,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.compactable_tools = compactable_tools
        self.cleared_content = cleared_content
        self.keep_recent = max(1, keep_recent)  # never clear the most recent result

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._over_trigger(messages):
            return messages
        clear_ids = self._ids_to_clear(messages)
        if not clear_ids:
            return messages

        out: list[dict[str, Any]] = []
        changed = False
        for message in messages:
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") in clear_ids
                and message.get("content") != self.cleared_content
            ):
                out.append({**message, "content": self.cleared_content})
                changed = True
            else:
                out.append(message)
        return out if changed else messages

    def _ids_to_clear(self, messages: list[dict[str, Any]]) -> set[str]:
        """Compactable tool_call_ids older than the last ``keep_recent``.

        Order is taken from the assistant ``tool_calls`` that issued them (the
        tool *name* lives on the call, not the ``role:"tool"`` answer).
        """
        compactable_ids: list[str] = []
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                name = call.get("function", {}).get("name")
                if name in self.compactable_tools and call.get("id"):
                    compactable_ids.append(call["id"])
        if len(compactable_ids) <= self.keep_recent:
            return set()
        return set(compactable_ids[: -self.keep_recent])


class OldHistorySnipShaper(_ReactiveHistoryShaper):
    """Layer A — cheapest history compaction: pure deletion of old turns.

    When the estimated view exceeds ``trigger_tokens``, drops whole old
    *tool-exchange* groups (an assistant ``tool_calls`` turn plus its results)
    oldest-first until the estimate falls to ``target_tokens`` or no such group
    remains. Tool exchanges are the bulky, lowest-reference-value middle of a
    long run; user turns and assistant text turns are left untouched (their
    decisions still carry value — that is what makes auto-compact the heavier
    fallback). Deletion only; no summary, no model call. The dropped content
    survives in ``state.messages`` / the transcript, so a resume rebuilds it.
    """

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._over_trigger(messages):
            return messages
        spans, lo, hi = _droppable_region(messages, self.keep_recent_groups)
        if lo >= hi:
            return messages

        running = self._estimate(messages)
        drop: set[int] = set()
        for gi in range(lo, hi):
            start, end = spans[gi]
            leader = messages[start]
            if not (leader.get("role") == "assistant" and leader.get("tool_calls")):
                continue  # preserve user / assistant-text turns
            drop.update(range(start, end))
            running -= self._estimate(messages[start:end])
            if running <= self.target_tokens:
                break

        if not drop:
            return messages
        return [m for i, m in enumerate(messages) if i not in drop]


class AutoCompactShaper(_ReactiveHistoryShaper):
    """Layer B — heaviest history compaction: model-generated summary.

    Last resort, default-off (``summarizer is None`` ⇒ identity). When still
    over ``trigger_tokens`` after cheaper layers, the whole droppable region is
    handed to the injected summarizer and replaced by a single *visible* marker
    message (``COMPACTED_MARKER_PREFIX``) — it announces itself as a compressed
    stand-in rather than masquerading as original history. The replacement spans
    whole groups, so the kept recent window still starts on a group boundary and
    no ``tool_call_id`` is orphaned. The original messages remain in
    ``state.messages`` / the transcript for a lossless resume.
    """

    def __init__(self, *, summarizer: SummarizerPort | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.summarizer = summarizer

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.summarizer is None or not self._over_trigger(messages):
            return messages
        spans, lo, hi = _droppable_region(messages, self.keep_recent_groups)
        if lo >= hi:
            return messages
        start, end = spans[lo][0], spans[hi - 1][1]
        segment = messages[start:end]
        if not segment:
            return messages
        marker = {
            "role": "system",
            "content": (
                f"{COMPACTED_MARKER_PREFIX} — summary of {len(segment)} earlier "
                f"messages]:\n{self.summarizer(segment)}"
            ),
            "compacted": True,
        }
        return [*messages[:start], marker, *messages[end:]]


class ContextCollapseShaper:
    """Layer C — reserved insertion point only (Liu et al. 2026 §4.3).

    Context collapse is a read-time projection over full history with boundary
    markers and chained reconstruction. Not implemented this period: this is an
    explicit identity placeholder so the pipeline already holds C's slot (after
    auto-compact) and a later upgrade only swaps the body.
    """

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages


__all__ = [
    "DEFAULT_TOOL_RESULT_BUDGET",
    "DEFAULT_HISTORY_TRIGGER_TOKENS",
    "DEFAULT_HISTORY_TARGET_TOKENS",
    "DEFAULT_HISTORY_KEEP_RECENT_GROUPS",
    "DEFAULT_CLEARED_TOOL_CONTENT",
    "DEFAULT_TOOL_CLEAR_KEEP_RECENT",
    "DEFAULT_COMPACTABLE_TOOLS",
    "DEFAULT_OUTPUT_RESERVE_TOKENS",
    "DEFAULT_COMPACT_BUFFER_TOKENS",
    "HISTORY_TARGET_RATIO",
    "COMPACTED_MARKER_PREFIX",
    "SummarizerPort",
    "approx_messages_tokens",
    "history_trigger_target",
    "ShaperPipeline",
    "PerToolResultBudgetShaper",
    "ToolOutputClearShaper",
    "OldHistorySnipShaper",
    "AutoCompactShaper",
    "ContextCollapseShaper",
]
