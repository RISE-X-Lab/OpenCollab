"""Preserve native thinking across append-only requests and context edits."""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable, Iterator, Set
from contextlib import contextmanager
from itertools import chain
from typing import Any

from opencollab.application.shaping.pipeline import (
    _group_spans,
    _history_shaping_stages,
    _HistoryShapingStage,
    _observe_history_shaping,
    is_complete_tool_exchange,
)
from opencollab.application.shaping.reactive import DEFAULT_CLEARED_TOOL_CONTENT, _snipped_exchange_text
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

def _anchor_key(group: list[dict]) -> tuple:
    message = group[0]
    calls = message.get("tool_calls")
    if (
        isinstance(calls, list) and all(isinstance(call, dict) for call in calls)
        and is_complete_tool_exchange(group, (0, len(group)))
    ):
        return ("exchange", tuple(str.__str__(call["id"]) for call in calls))
    role = message.get("role")
    content = message.get("tool_call_id", message.get("content"))
    if isinstance(content, list) and content and isinstance(content[0], dict):
        content = content[0].get("text")
    return (
        "message", str.__str__(role) if isinstance(role, str) else None,
        str.__str__(content) if isinstance(content, str) else None,
    )

def _exchange_leader_key(group: list[dict]) -> tuple | None:
    leader = group[0]
    content = leader.get("content")
    if content is not None and not isinstance(content, str):
        return None
    calls = []
    for call in leader["tool_calls"]:
        function = call.get("function")
        if not isinstance(function, dict):
            return None
        name, arguments = function.get("name"), function.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, str):
            return None
        calls.append((str.__str__(name), str.__str__(arguments)))
    return (str.__str__(content) if content is not None else None, tuple(calls))

def _ordered_anchors(candidates: list[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    # A moved group can cross several candidates. Keep the anchors on both sides
    # of the move and let the caller rebuild the reordered region from the source.
    highest = -1
    increasing = set()
    for old, new in candidates:
        if new > highest:
            increasing.add(old)
            highest = new
    lowest = float("inf")
    ordered = []
    for old, new in reversed(candidates):
        if new < lowest and old in increasing:
            ordered.append((old, new))
        lowest = min(lowest, new)
    yield from reversed(ordered)

def _shared_result_anchors(
    before: list[list[dict]], after: list[list[dict]],
    old_keys: list[tuple], new_keys: list[tuple], repeated: set[tuple],
) -> dict[int, int]:
    # A retained raw result can identify a batch whose other result was edited.
    def occurrences(groups: list[list[dict]], keys: list[tuple]) -> dict[tuple, tuple[int, dict] | None]:
        found: dict[tuple, tuple[int, dict] | None] = {}
        for index, (group, key) in enumerate(zip(groups, keys)):
            if key not in repeated or len(group) <= 2:
                continue
            for result in group[1:]:
                content = result.get("content")
                if isinstance(content, str):
                    result_key = (key, str.__str__(result["tool_call_id"]), str.__str__(content))
                    found[result_key] = None if result_key in found else (index, result)
        return found

    old_results, new_results = occurrences(before, old_keys), occurrences(after, new_keys)
    old_matches: dict[int, set[int]] = {}
    new_matches: dict[int, set[int]] = {}
    for key, previous in old_results.items():
        current = new_results.get(key)
        if previous is None or current is None:
            continue
        old, old_result = previous
        new, new_result = current
        if same_snapshot_value(old_result, new_result) and same_snapshot_value(before[old][0], after[new][0]):
            old_matches.setdefault(old, set()).add(new)
            new_matches.setdefault(new, set()).add(old)
    return {
        old: new for old, matches in old_matches.items() if len(matches) == 1
        for new in matches if len(new_matches[new]) == 1
    }

def _align_groups(
    before: list[list[dict]], after: list[list[dict]], *,
    projected: bool = False, match_edited_results: bool = False,
) -> Iterator[tuple[int, int]]:
    old_keys = list(map(_anchor_key, before))
    new_keys = list(map(_anchor_key, after))
    old_counts, new_counts = Counter(old_keys), Counter(new_keys)
    if projected or match_edited_results:
        # Result clearing keeps the leader even when a provider reuses its call IDs.
        repeated = {
            key for key in old_counts.keys() | new_counts.keys()
            if key[0] == "exchange" and (old_counts[key] > 1 or new_counts[key] > 1)
        }
        if repeated:
            old_keys = [(*key, _exchange_leader_key(group)) if key in repeated else key
                        for key, group in zip(old_keys, before)]
            new_keys = [(*key, _exchange_leader_key(group)) if key in repeated else key
                        for key, group in zip(new_keys, after)]
            old_counts, new_counts = Counter(old_keys), Counter(new_keys)
    result_anchors: dict[int, int] = {}
    repeated_results: set[tuple] = set()
    if match_edited_results:
        repeated_results = {
            key for key in old_counts.keys() & new_counts.keys()
            if key[0] == "exchange" and (old_counts[key] > 1 or new_counts[key] > 1)
        }
        if repeated_results:
            result_anchors = _shared_result_anchors(before, after, old_keys, new_keys, repeated_results)
    result_targets = set(result_anchors.values())

    def matches(old: int, new: int) -> bool:
        key = old_keys[old]
        if key != new_keys[new]:
            return False
        if same_snapshot_value(before[old], after[new]):
            return True
        if match_edited_results:
            return (
                key[0] == "exchange" and len(before[old]) > 2
                and (old_counts[key] == new_counts[key] == 1 or result_anchors.get(old) == new)
                and same_snapshot_value(before[old][0], after[new][0])
            )
        if not projected:
            return False
        if key[0] == "exchange":
            if old_counts[key] != 1 or new_counts[key] != 1:
                return False
            if len(key) == 2:
                return True
            return key[2] is not None and same_snapshot_value(
                _strip_anthropic_thinking(before[old][:1]), _strip_anthropic_thinking(after[new][:1]),
            )
        return same_snapshot_value(_strip_anthropic_thinking(before[old]), after[new])

    def same_locally_anchored_batch(old: int, new: int) -> bool:
        key = old_keys[old]
        if (
            key != new_keys[new] or len(before[old]) <= 2
            or old in result_anchors or new in result_targets
            or not same_snapshot_value(before[old][0], after[new][0])
        ):
            return False
        for groups, keys, index, group in (
            (before, old_keys, old, after[new]), (after, new_keys, new, before[old]),
        ):
            if any(
                other != index and keys[other] == key and same_snapshot_value(candidate, group)
                for other, candidate in enumerate(groups)
            ):
                return False
        current_rows = _projected_tool_results(before[old], after[new])
        return current_rows is not None and any(
            same_snapshot_value(row, current_rows[row["tool_call_id"]]) for row in before[old][1:]
        )

    def bracketed_result_edits(left: int, right: int, old_stop: int, new_stop: int) -> list[tuple[int, int]]:
        if left == 0 or right == 0 or old_stop == len(before) or new_stop == len(after):
            return []
        neighbors = (old_keys[left - 1], old_keys[old_stop])
        if not any(old_counts[neighbor] == new_counts[neighbor] == 1 for neighbor in neighbors):
            return []
        if not same_snapshot_value(before[left - 1], after[right - 1]) or not same_snapshot_value(
            before[old_stop], after[new_stop],
        ):
            return []
        old_batch = [index for index in range(left, old_stop) if old_keys[index] in repeated_results]
        new_batch = [index for index in range(right, new_stop) if new_keys[index] in repeated_results]
        if len(old_batch) != len(new_batch):
            return []
        pairs = list(zip(old_batch, new_batch))
        return pairs if all(same_locally_anchored_batch(old, new) for old, new in pairs) else []

    def equal_edges(old: int, new: int, old_stop: int, new_stop: int) -> tuple[int, int]:
        size = min(old_stop - old, new_stop - new)
        prefix = suffix = 0
        while prefix < size and matches(old + prefix, new + prefix):
            prefix += 1
        while suffix < size - prefix and matches(old_stop - suffix - 1, new_stop - suffix - 1):
            suffix += 1
        return prefix, suffix

    # Matching runs at the ends can anchor repeated text without a unique key.
    prefix, suffix = equal_edges(0, 0, len(before), len(after))
    yield from ((index, index) for index in range(prefix))
    old_stop, new_stop = len(before) - suffix, len(after) - suffix
    old_middle, new_middle = old_keys[prefix:old_stop], new_keys[prefix:new_stop]
    old_counts_middle, new_counts_middle = Counter(old_middle), Counter(new_middle)
    new_positions = {
        key: index for index, key in enumerate(new_middle, prefix) if new_counts_middle[key] == 1
    }
    candidates = [
        (old, new_positions[key]) for old, key in enumerate(old_middle, prefix)
        if old_counts_middle[key] == 1 and key in new_positions and matches(old, new_positions[key])
    ]
    if result_anchors:
        candidates = sorted(set(candidates) | {
            (old, new) for old, new in result_anchors.items()
            if prefix <= old < old_stop and prefix <= new < new_stop
        })

    old = new = prefix
    for next_old, next_new in chain(_ordered_anchors(candidates), ((old_stop, new_stop),)):
        head, tail = equal_edges(old, new, next_old, next_new)
        for offset in range(head):
            yield old + offset, new + offset
        local_pairs = (
            bracketed_result_edits(old + head, new + head, next_old - tail, next_new - tail)
            if repeated_results and old + head + tail < next_old and new + head + tail < next_new else []
        )
        if next_old - old == next_new - new and all(i - old == j - new for i, j in local_pairs):
            local_matches = set(local_pairs)
            for offset in range(head, next_old - old - tail):
                pair = old + offset, new + offset
                if pair in local_matches or matches(*pair):
                    yield pair
        else:
            yield from local_pairs
        for offset in range(-tail, 0):
            yield next_old + offset, next_new + offset
        if next_old < old_stop:
            yield next_old, next_new
        old, new = next_old + 1, next_new + 1
    yield from ((old_stop + offset, new_stop + offset) for offset in range(suffix))

def _projected_tool_results(original: list[dict], shaped: list[dict]) -> dict[str, dict] | None:
    key = _anchor_key(original)
    if key[0] != "exchange" or key != _anchor_key(shaped) or not same_snapshot_value(
        _strip_anthropic_thinking(original[:1]), _strip_anthropic_thinking(shaped[:1]),
    ):
        return None
    results = {row["tool_call_id"]: row for row in shaped[1:]}
    for row in original[1:]:
        result = results[row["tool_call_id"]]
        if not same_snapshot_value({**row, "content": result.get("content")}, result):
            return None
    return results

def _is_cleared_exchange_run(source: list[list[dict]], projection: list[list[dict]]) -> bool:
    originals: dict[str, set[str]] = {}
    cleared: set[str] = set()
    marker = None
    count = 0
    for original, shaped in zip(source, projection, strict=True):
        results = _projected_tool_results(original, shaped)
        if results is None:
            return False
        for row in original[1:]:
            call_id = row["tool_call_id"]
            before = row.get("content")
            after = results[call_id].get("content")
            if isinstance(before, str):
                originals.setdefault(call_id, set()).add(str.__str__(before))
            if same_snapshot_value(before, after):
                continue
            if not isinstance(before, str) or not isinstance(after, str) or len(after) >= len(before):
                return False
            if marker is not None and not same_snapshot_value(after, marker):
                return False
            marker = str.__str__(after)
            count += 1
            cleared.add(call_id)
    if marker is None:
        return True
    # A configured clearing marker repeats and cannot also be a moved original result.
    return (count > 1 or marker == DEFAULT_CLEARED_TOOL_CONTENT) and not any(
        marker in originals[call_id] for call_id in cleared
    )

def _rebase_tool_results(original: list[dict], shaped: list[dict], current: list[dict]) -> list[dict] | None:
    results = _projected_tool_results(original, shaped)
    if results is None or not same_snapshot_value(original[0], current[0]):
        return None
    original_results = {row["tool_call_id"]: row for row in original[1:]}
    rebased = [shaped[0]]
    for row in current[1:]:
        call_id = row["tool_call_id"]
        rebased.append(results[call_id] if same_snapshot_value(original_results[call_id], row) else row)
    return rebased

def _groups(rows: list[dict]) -> list[list[dict]]:
    return [rows[start:stop] for start, stop in _group_spans(rows)]

def _groups_with_snips(rows: list[dict], snipped: Set[int]) -> Iterator[tuple[int, list[dict]]]:
    for start, stop in _group_spans(rows):
        if start not in snipped:
            yield start, rows[start:stop]
        elif (text := _snipped_exchange_text(rows[start])) is not None:
            yield start, [text]

def _retained_snips(state: SessionState) -> set[int]:
    history = state.anthropic_history
    snipped = history.get("snipped_exchanges") if history is not None else None
    if not snipped:
        return set()
    if not _anthropic_source_changed(state):
        return set(snipped)
    snipped = set(snipped)
    source = history["source_messages"]
    old_spans = _group_spans(source)
    new_spans = _group_spans(state.messages)
    return {
        new_spans[new][0] for old, new in _align_groups(_groups(source), _groups(state.messages))
        if old_spans[old][0] in snipped
    }

@contextmanager
def _capture_anthropic_snips(
    state: SessionState | None,
) -> Iterator[Callable[[list[dict], list[dict]], set[int]]]:
    transitions: dict[int, tuple[list[dict], list[dict], set[int] | None]] = {}
    stage_results: dict[_HistoryShapingStage, list[list[dict]]] = {}
    snipped: set[int] = set()

    def snip_path(initial: list[dict], shaped: list[dict]) -> list[tuple[list[dict], set[int]]] | None:
        adopted: list[tuple[list[dict], set[int]]] = []
        visited: set[int] = set()
        while shaped is not initial:
            key = id(shaped)
            transition = transitions.get(key)
            if transition is None or key in visited:
                return None
            visited.add(key)
            before, _, starts = transition
            if starts:
                adopted.append((before, starts))
            shaped = before
        return adopted

    def copied_result_input(before: list[dict], after: list[dict], candidates: list[list[dict]]) -> list[dict]:
        matched = before
        path = None
        for result in candidates:
            if not same_snapshot_value(result, after):
                continue
            candidate = snip_path(before, result)
            if candidate is None:
                continue
            if path is not None and (
                len(path) != len(candidate) or any(
                    old_starts != new_starts or not same_snapshot_value(old_rows, new_rows)
                    for (old_rows, old_starts), (new_rows, new_starts) in zip(path, candidate)
                )
            ):
                return before
            matched, path = result, candidate
        return matched if path else before

    def observe(before: list[dict], after: list[dict], starts: set[int] | None) -> None:
        result_key = id(after)
        known_result = result_key in transitions
        stage = _history_shaping_stages.get()
        candidates = stage_results.get(stage[-1], []) if stage else []
        if known_result and (starts is not None or all(result is not after for result in candidates)):
            return
        if id(before) not in transitions:
            # A wrapper may give its inner shaper a fresh copy of the input.
            before = next((entry.messages for entry in stage if same_snapshot_value(entry.messages, before)), before)
        if before is after:
            return
        # An unchanged stage preserves the input's ancestry even if a trial
        # produced the same value through a different sequence of transformations.
        if starts is None and candidates and not same_snapshot_value(before, after):
            if known_result:
                return
            before = copied_result_input(before, after, candidates)
        transitions[result_key] = (before, after, starts)
        if not known_result:
            for entry in stage:
                stage_results.setdefault(entry, []).append(after)

    def record(before: list[dict], starts: set[int]) -> None:
        assert state is not None
        entries = list(_groups_with_snips(state.messages, _retained_snips(state) | snipped))
        candidates = [group for _, group in entries]
        before_spans = _group_spans(before)
        projected = _groups(before)
        if list(map(_anchor_key, candidates)) == list(map(_anchor_key, projected)):
            # The earlier budget and clearing layers keep occurrences in order.
            aligned = (
                (index, index) for index, (old, new) in enumerate(zip(candidates, projected))
                if same_snapshot_value(old[0], new[0])
                or same_snapshot_value(_strip_anthropic_thinking(old)[0], new[0])
            )
        else:
            aligned = _align_groups(candidates, projected, projected=True)
        snipped.update(entries[old][0] for old, new in aligned if before_spans[new][0] in starts)

    def collect(initial: list[dict], shaped: list[dict]) -> set[int]:
        for before, starts in reversed(snip_path(initial, shaped) or ()):
            record(before, starts)
        return snipped

    # Collect only transformations on the path to the history used by the request.
    token = _observe_history_shaping.set(observe if state is not None else None)
    try:
        yield collect
    finally:
        _observe_history_shaping.reset(token)
        transitions.clear()
        stage_results.clear()

def _rebase_anthropic_history(state: SessionState) -> tuple[list[dict], set[int]]:
    history = state.anthropic_history
    assert history is not None
    view = history["messages"]
    retained = _retained_snips(state)
    # A later summary only saw the text left behind by earlier snips.
    source = [group for _, group in _groups_with_snips(
        history["source_messages"], set(history.get("snipped_exchanges", ())),
    )]
    projection = _groups(view)
    current = [group for _, group in _groups_with_snips(state.messages, retained)]
    aligned = dict(_align_groups(source, current, match_edited_results=True))
    unchanged = {old: new for old, new in aligned.items() if same_snapshot_value(source[old], current[new])}
    first_edit = 0
    while unchanged.get(first_edit) == first_edit:
        first_edit += 1
    end_edit = len(source)
    last_match = unchanged.get(end_edit - 1)
    while end_edit > first_edit and last_match is not None and unchanged.get(end_edit - 1) == last_match:
        end_edit -= 1
        last_match -= 1
    source_counts = Counter(map(_anchor_key, source))
    projection_counts = Counter(map(_anchor_key, projection))

    def bracketed_exchanges(old: int, seen: int, next_old: int, next_seen: int) -> bool:
        if old == 0 or next_old == len(source) or next_old - old != next_seen - seen:
            return False
        # A repeated acknowledgment can flank a call when the other neighbor is unique.
        has_unique_anchor = False
        for left, right in ((old - 1, seen - 1), (next_old, next_seen)):
            key = _anchor_key(source[left])
            unique = source_counts[key] == projection_counts[key] == 1
            if (key[0] == "exchange" and not unique) or not same_snapshot_value(
                _strip_anthropic_thinking(source[left]), _strip_anthropic_thinking(projection[right]),
            ):
                return False
            has_unique_anchor = has_unique_anchor or unique
        if not has_unique_anchor:
            return False
        if next_old == old + 1:
            return _projected_tool_results(source[old], projection[seen]) is not None
        # Clearing within a run preserves the position and identity of every batch.
        return _is_cleared_exchange_run(source[old:next_old], projection[seen:next_seen])

    def matched_exchange(index: int, *, contiguous: bool) -> bool:
        current_index = aligned.get(index)
        if current_index is None:
            return False
        if contiguous or index not in unchanged:
            return True
        if unchanged.get(index - 1) == current_index - 1 and unchanged.get(index + 1) == current_index + 1:
            return True
        # An edited neighbor can still bound the saved projection. Match the
        # raw occurrence independently before carrying its projection forward.
        return all(
            sum(same_snapshot_value(group, source[index]) for group in groups) == 1
            for groups in (source, current)
        )

    def projected_groups() -> Iterator[tuple[int, int, list[list[dict]]]]:
        old = seen = 0
        anchors = _align_groups(source, projection, projected=True)
        for next_old, next_seen in chain(anchors, ((len(source), len(projection)),)):
            if old < next_old:
                gap = projection[seen:next_seen]
                if not gap and all(_anchor_key(group)[0] == "exchange" for group in source[old:next_old]):
                    for index in range(old, next_old):
                        yield index, index + 1, []
                elif bracketed_exchanges(old, seen, next_old, next_seen):
                    current_start = aligned.get(old)
                    contiguous = next_old > old + 1 and current_start is not None and all(
                        aligned.get(index) == current_start + index - old for index in range(old, next_old)
                    )
                    for offset, group in enumerate(gap):
                        index = old + offset
                        if matched_exchange(index, contiguous=contiguous):
                            yield index, index + 1, [group]
                elif (
                    next_old <= first_edit or old >= end_edit
                    or all(
                        key[0] != "exchange" or source_counts[key] == projection_counts[key] == 1
                        for key in map(_anchor_key, gap)
                    )
                ):
                    yield old, next_old, gap
            if next_old < len(source):
                yield next_old, next_old + 1, [projection[next_seen]]
            old, seen = next_old + 1, next_seen + 1

    rebased: list[dict] = []
    taken = 0
    for start, stop, replacement in projected_groups():
        start_new = unchanged.get(start)
        # A summary covers its whole source interval, including possible insertions.
        if start_new is None or any(unchanged.get(index) != start_new + index - start for index in range(start, stop)):
            start_new = aligned.get(start)
            if start_new is None or stop != start + 1 or len(replacement) != 1:
                continue
            exchange = _rebase_tool_results(source[start], replacement[0], current[start_new])
            if exchange is None:
                continue
            replacement = [exchange]
        rebased.extend(chain.from_iterable(current[taken:start_new]))
        rebased.extend(chain.from_iterable(replacement))
        taken = start_new + stop - start
    rebased.extend(chain.from_iterable(current[taken:]))
    return _edited_anthropic_history(view, rebased), retained

def _anthropic_history_input(state: SessionState) -> list[dict]:
    history = state.anthropic_history
    if history is None:
        return state.messages
    if _anthropic_source_changed(state):
        return copy.deepcopy(_rebase_anthropic_history(state)[0])
    return copy.deepcopy([*history["messages"], *state.messages[history["source_count"]:]])

def _remember_anthropic_history(
    state: SessionState, messages: list[dict], tools: list[dict] | None,
    snipped_exchanges: set[int] | None = None,
) -> list[dict]:
    """Persist edits so removed blocks and compacted content stay removed."""
    history = state.anthropic_history
    messages = _edited_anthropic_history(_anthropic_history_input(state), messages)
    if history is not None:
        messages = _edited_anthropic_history(history["messages"], messages)
        if not same_snapshot_value(history["tools"], tools):
            messages = _strip_anthropic_thinking(messages)
    snipped = _retained_snips(state) | (snipped_exchanges or set())
    state.anthropic_history = copy.deepcopy({
        "messages": messages, "source_count": len(state.messages), "tools": tools,
        "source_messages": state.messages,
    })
    if snipped:
        state.anthropic_history["snipped_exchanges"] = sorted(snipped)
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
