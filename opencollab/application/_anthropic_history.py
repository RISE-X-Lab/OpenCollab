"""Preserve native thinking across append-only requests and context edits."""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from opencollab.application.shaping.pipeline import _group_spans, is_complete_tool_exchange
from opencollab.domain.session import SessionState, same_snapshot_value

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

def _strip_anthropic_thinking(messages: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """Remove a suffix of native thinking while retaining text and tool blocks."""
    result = list(messages)
    for index in range(start, len(result)):
        message = result[index]
        state = message.get("provider_state")
        blocks = state.get("anthropic_content") if isinstance(state, dict) else None
        if not isinstance(blocks, list):
            continue
        kept = [block for block in blocks if not isinstance(block, dict) or block.get("type") not in _THINKING_TYPES]
        if len(kept) != len(blocks):
            result[index] = {**message, "provider_state": {**state, "anthropic_content": kept}}
    return result

def _edited_anthropic_history(before: list[dict], after: list[dict]) -> list[dict]:
    """Drop thinking bound to a changed prefix, keeping the valid older run."""
    before_system = [message for message in before if message.get("role") == "system" and not message.get("compacted")]
    after_system = [message for message in after if message.get("role") == "system" and not message.get("compacted")]
    if not same_snapshot_value(before_system, after_system):
        return _strip_anthropic_thinking(after)
    for index, (old, new) in enumerate(zip(before, after)):
        if not same_snapshot_value(old, new):
            return _strip_anthropic_thinking(after, index)
    return after

def _anthropic_source_changed(state: SessionState) -> bool:
    history = state.anthropic_history
    source = history.get("source_messages") if history is not None else None
    return source is not None and not same_snapshot_value(state.messages[:len(source)], source)

def _anchor_key(message: dict) -> tuple[str | None, str | None]:
    role = message.get("role")
    content = message.get("tool_call_id", message.get("content"))
    if isinstance(content, list) and content and isinstance(content[0], dict):
        content = content[0].get("text")
    return (
        str.__str__(role) if isinstance(role, str) else None,
        str.__str__(content) if isinstance(content, str) else None,
    )

def _projection_anchor(
    source: list[dict], view: list[dict], index: int, counts: Counter[tuple[str | None, str | None]],
) -> int | None:
    offset = len(view) - len(source)
    if offset and counts[_anchor_key(source[index])] != 1:
        return None
    candidates = (index, index + offset) if offset else (index,)
    matches = [
        candidate for candidate in candidates
        if 0 <= candidate < len(view) and same_snapshot_value(source[index], view[candidate])
    ]
    return matches[0] if len(matches) == 1 else None

def _rebuild_edited_span(source: list[dict], view: list[dict], messages: list[dict]) -> list[dict]:
    start = 0
    while start < min(len(source), len(messages)) and same_snapshot_value(source[start], messages[start]):
        start += 1

    # Exact anchors keep independent compaction intact. A summary or transformed
    # message between the edit and its nearest anchors may depend on the edit.
    counts = Counter(map(_anchor_key, source)) if len(view) != len(source) else Counter()
    view_start = source_start = 0
    for index in range(min(start, len(source) - 1), -1, -1):
        anchor = _projection_anchor(source, view, index, counts)
        if anchor is not None:
            keep = int(index < start)
            view_start, source_start = anchor + keep, index + keep
            break

    if len(messages) >= len(source):
        stop = len(source)
        while stop > start and same_snapshot_value(source[stop - 1], messages[stop - 1]):
            stop -= 1
        for index in range(stop - 1, len(source)):
            anchor = _projection_anchor(source, view, index, counts)
            if anchor is not None:
                skip = int(index < stop)
                view_stop, source_stop = anchor + skip, index + skip
                if view_stop >= view_start and source_stop >= source_start:
                    return [
                        *view[:view_start], *messages[source_start:source_stop],
                        *view[view_stop:], *messages[len(source):],
                    ]
    return [*view[:view_start], *messages[source_start:]]

def _tool_exchange_spans(messages: list[dict]) -> dict[tuple[str, ...], tuple[int, int] | None]:
    exchanges: dict[tuple[str, ...], tuple[int, int] | None] = {}
    for start, stop in _group_spans(messages):
        calls = messages[start].get("tool_calls")
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            continue
        if is_complete_tool_exchange(messages, (start, stop)):
            call_ids = tuple(call["id"] for call in calls)
            exchanges[call_ids] = None if call_ids in exchanges else (start, stop)
    return exchanges

def _rebase_anthropic_history(history: dict, messages: list[dict]) -> list[dict]:
    source = history["source_messages"]
    view = history["messages"]
    rebased = _rebuild_edited_span(source, view, messages)
    projections = _tool_exchange_spans(view)
    if projections:
        previous = _tool_exchange_spans(source)
        current = _tool_exchange_spans(messages)
        for call_ids, target in _tool_exchange_spans(rebased).items():
            old = previous.get(call_ids)
            new = current.get(call_ids)
            projection = projections.get(call_ids)
            if target is None or old is None or new is None or projection is None:
                continue
            if same_snapshot_value(source[old[0]:old[1]], messages[new[0]:new[1]]):
                # Clear and trim shapers preserve the exchange while shortening its results.
                rebased[target[0]:target[1]] = view[projection[0]:projection[1]]
    return _edited_anthropic_history(view, rebased)

def _anthropic_history_input(state: SessionState) -> list[dict]:
    history = state.anthropic_history
    if history is None:
        return state.messages
    if _anthropic_source_changed(state):
        return copy.deepcopy(_rebase_anthropic_history(history, state.messages))
    return copy.deepcopy([*history["messages"], *state.messages[history["source_count"]:]])

def _remember_anthropic_history(
    state: SessionState, messages: list[dict], tools: list[dict] | None,
) -> list[dict]:
    """Persist edits so removed blocks and compacted content stay removed."""
    history = state.anthropic_history
    messages = _edited_anthropic_history(_anthropic_history_input(state), messages)
    if history is not None:
        messages = _edited_anthropic_history(history["messages"], messages)
        if not same_snapshot_value(history["tools"], tools):
            messages = _strip_anthropic_thinking(messages)
    state.anthropic_history = copy.deepcopy({
        "messages": messages, "source_count": len(state.messages), "tools": tools,
        "source_messages": state.messages,
    })
    return messages

def _recover_anthropic_history(
    state: SessionState, messages: list[dict], tools: list[dict] | None, error: Exception,
) -> tuple[list[dict], int] | None:
    """Retire replayed thinking after an explicit provider prefix rejection."""
    status = getattr(error, "status_code", None)
    if status != 400:
        return None
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(detail, dict) or detail.get("type") != "invalid_request_error":
        return None
    message = detail.get("message")
    if not isinstance(message, str) or (
        "Invalid `signature` in `thinking` block. The block is bound to a different conversation."
        not in message
    ):
        return None
    recovered = _strip_anthropic_thinking(messages)
    removed = sum(
        len(old["provider_state"]["anthropic_content"]) - len(new["provider_state"]["anthropic_content"])
        for old, new in zip(messages, recovered) if old is not new
    )
    if not removed:
        return None
    return _remember_anthropic_history(state, recovered, tools), removed
