"""Reactive history-compaction layers (lazy-degradation chain A0/A/B/C).

These layers no-op until the estimated context crosses a trigger, then degrade
progressively: clear old tool *content* in place (lowest loss) → snip whole old
tool-exchange turns → auto-compact (model-generated summary, default-off) →
reserved collapse slot. Every layer is a read-time projection over a *copy*:
``state.messages`` and the persisted transcript keep the full original history.
"""

from __future__ import annotations

from typing import Any, Callable

from opencollab.application.ports import TokenEstimatorPort
from opencollab.application.shaping.pipeline import (
    DEFAULT_HISTORY_KEEP_RECENT_GROUPS,
    DEFAULT_HISTORY_TARGET_TOKENS,
    DEFAULT_HISTORY_TRIGGER_TOKENS,
    _droppable_region,
    approx_messages_tokens,
)

# Tool-output clearing (ToolOutputClearShaper). Old results from these bulky,
# reconstructible read-only tools have their *content* replaced in place; the
# last ``KEEP_RECENT`` compactable results stay verbatim. Names are real
# OpenCollab tool names (see ``bootstrap/container.py``), not a hardcoded set.
DEFAULT_CLEARED_TOOL_CONTENT = "[Old tool result content cleared]"
DEFAULT_TOOL_CLEAR_KEEP_RECENT = 5
DEFAULT_COMPACTABLE_TOOLS = frozenset(
    {"bash", "file_read", "grep", "git_diff", "run_tests"}
)

# Visible marker prefix for an auto-compacted segment — the summary announces
# itself as a compressed stand-in rather than masquerading as original history
# (Liu et al. 2026 §11.3: no invisible compression).
COMPACTED_MARKER_PREFIX = "[Context auto-compacted"

# A synchronous summarizer: given a contiguous message segment, return summary
# prose. Kept sync because ``ShaperPort.shape`` is sync; ``None`` disables the
# auto-compact layer (the default-off switch).
SummarizerPort = Callable[[list[dict[str, Any]]], str]


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
