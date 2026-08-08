"""Reactive history-compaction layers (lazy-degradation chain A0/A/B).

These layers no-op until the estimated context crosses a trigger, then degrade
progressively: clear old tool *content* in place (lowest loss) → snip whole old
tool-exchange turns → auto-compact (model-generated summary, default-off). Every
layer is a read-time projection over a *copy*: ``state.messages`` and the
persisted transcript keep the full original history.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Callable

from opencollab.application.ports import TokenEstimatorPort
from opencollab.application.shaping.pipeline import (
    DEFAULT_HISTORY_KEEP_RECENT_GROUPS,
    DEFAULT_HISTORY_TARGET_TOKENS,
    DEFAULT_HISTORY_TRIGGER_TOKENS,
    _droppable_region,
    approx_messages_tokens,
    is_complete_tool_exchange,
    matched_tool_result_occurrences,
    pinned_free_region,
    require_nonnegative_int,
    require_positive_int,
    span_is_safe_to_compact,
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
DEFAULT_AUTOCOMPACT_CACHE_SIZE = 8

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
        require_positive_int(trigger_tokens, "trigger_tokens")
        require_positive_int(target_tokens, "target_tokens")
        if target_tokens >= trigger_tokens:
            raise ValueError("target_tokens must be smaller than trigger_tokens")
        require_nonnegative_int(keep_recent_groups, "keep_recent_groups")
        self._estimate = estimate_tokens
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
        self.keep_recent_groups = keep_recent_groups
        # When set, every layer acts as if the trigger were crossed: it compacts
        # unconditionally toward ``target_tokens`` instead of no-op'ing below the
        # estimate. Toggled only by the forced-compaction safety-net pass (see
        # ``pipeline.forced_shape``) after a real context-overflow rejection,
        # where the char estimate provably under-counted the prompt.
        self._forced = False

    def _over_trigger(self, messages: list[dict[str, Any]]) -> bool:
        if self._forced:
            return bool(messages)
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
        require_positive_int(keep_recent, "keep_recent")
        self.compactable_tools = compactable_tools
        self.cleared_content = cleared_content
        self.keep_recent = keep_recent

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._over_trigger(messages):
            return messages
        clear_indices = self._indices_to_clear(messages)
        if not clear_indices:
            return messages

        out: list[dict[str, Any]] = []
        changed = False
        for index, message in enumerate(messages):
            if (
                message.get("role") == "tool"
                and index in clear_indices
                and message.get("content") != self.cleared_content
            ):
                out.append({**message, "content": self.cleared_content})
                changed = True
            else:
                out.append(message)
        return out if changed else messages

    def _indices_to_clear(self, messages: list[dict[str, Any]]) -> set[int]:
        """Exact compactable result occurrences older than ``keep_recent``.

        Pairing is local to each assistant turn, so reused ids cannot clear a
        newer result. Replacing a short body with a longer marker is forbidden.
        """
        compactable = [
            result_index
            for result_index, _call_id, name, _arguments
            in matched_tool_result_occurrences(messages)
            if name in self.compactable_tools
        ]
        if len(compactable) <= self.keep_recent:
            return set()
        return {
            result_index
            for result_index in compactable[: -self.keep_recent]
            if isinstance(messages[result_index].get("content"), str)
            and len(self.cleared_content) < len(messages[result_index]["content"])
        }


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
        replacements: dict[int, dict[str, Any]] = {}
        for gi in range(lo, hi):
            start, end = spans[gi]
            leader = messages[start]
            if not (leader.get("role") == "assistant" and leader.get("tool_calls")):
                continue  # preserve user / assistant-text turns
            if not is_complete_tool_exchange(messages, spans[gi]):
                continue

            content = leader.get("content")
            reasoning = leader.get("reasoning_content") or leader.get("reasoning")
            if reasoning and not content:
                continue  # cannot safely split provider-signed reasoning
            replacement = None
            if content:
                replacement = {
                    key: value for key, value in leader.items() if key != "tool_calls"
                }
                replacements[start] = replacement
                drop.update(range(start + 1, end))
            else:
                drop.update(range(start, end))

            removed = self._estimate(messages[start:end])
            retained = self._estimate([replacement]) if replacement is not None else 0
            running -= max(0, removed - retained)
            if running <= self.target_tokens:
                break

        if not drop:
            return messages
        return [
            replacements.get(index, message)
            for index, message in enumerate(messages)
            if index not in drop
        ]


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

    def __init__(
        self,
        *,
        summarizer: SummarizerPort | None = None,
        summary_cache_size: int = DEFAULT_AUTOCOMPACT_CACHE_SIZE,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        require_nonnegative_int(summary_cache_size, "summary_cache_size")
        self.summarizer = summarizer
        self._summary_cache_size = summary_cache_size
        self._summary_cache: OrderedDict[str, str] = OrderedDict()

    def _summary_cache_key(self, segment: list[dict[str, Any]]) -> str:
        """Hash normalized input plus the summarizer's model/prompt namespace."""
        assert self.summarizer is not None
        namespace = getattr(self.summarizer, "cache_key", None)
        if callable(namespace):
            namespace = namespace()
        payload = json.dumps(
            segment,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
        material = f"{id(self.summarizer)}\0{namespace!r}\0{payload}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _summarize(self, segment: list[dict[str, Any]]) -> str:
        assert self.summarizer is not None
        if self._summary_cache_size <= 0:
            return self.summarizer(segment)

        key = self._summary_cache_key(segment)
        cached = self._summary_cache.get(key)
        if cached is not None:
            self._summary_cache.move_to_end(key)
            return cached

        summary = self.summarizer(segment)
        cacheable = bool(summary) and bool(
            getattr(self.summarizer, "last_call_cacheable", True)
        )
        if cacheable:
            self._summary_cache[key] = summary
            self._summary_cache.move_to_end(key)
            while len(self._summary_cache) > self._summary_cache_size:
                self._summary_cache.popitem(last=False)
        return summary

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.summarizer is None or not self._over_trigger(messages):
            return messages
        spans, lo, hi = _droppable_region(messages, self.keep_recent_groups)
        # Never fold a pinned source (identity/team/task) into the summary.
        lo, hi = pinned_free_region(messages, spans, lo, hi)
        while lo < hi and not span_is_safe_to_compact(messages, spans[lo]):
            lo += 1
        safe_end = lo
        while safe_end < hi and span_is_safe_to_compact(messages, spans[safe_end]):
            safe_end += 1
        hi = safe_end
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
                f"messages]:\n{self._summarize(segment)}"
            ),
            "compacted": True,
        }
        candidate = [*messages[:start], marker, *messages[end:]]
        before = self._estimate(messages)
        after = self._estimate(candidate)
        if after >= before or after > self.target_tokens:
            return messages
        return candidate
