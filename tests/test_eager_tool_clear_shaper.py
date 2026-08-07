"""Unit tests for the always-on, cache-ready EagerToolOutputClearShaper.

These tests assert the three cache-ready properties explicitly — DETERMINISTIC,
MONOTONIC, IDEMPOTENT — plus the structural guarantees (pinned / non-compactable
untouched, ``tool_call`` pairing preserved, input never mutated).
"""

from __future__ import annotations

import copy

import pytest

from opencollab.application.shaping import (
    EAGER_STUB_PREFIX,
    EagerToolOutputClearShaper,
)
from opencollab.application.shaping.pipeline import PIN_FLOOR


def _sys():
    return {"role": "system", "content": "s"}


def _call(tid, name="bash", arguments="{}"):
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": tid, "function": {"name": name, "arguments": arguments}}
        ],
    }


def _tool(tid, content):
    return {"role": "tool", "tool_call_id": tid, "content": content}


def _orphaned_tool_ids(messages):
    call_ids = {
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls", [])
    }
    result_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    return result_ids - call_ids


def _exchange(tid, name="bash", arguments="{}", body="x" * 500):
    return [_call(tid, name=name, arguments=arguments), _tool(tid, body)]


def _history(n, keep_names=None):
    """A system seed plus ``n`` compactable bash tool exchanges t1..tn."""
    out = [_sys()]
    for i in range(1, n + 1):
        out += _exchange(f"t{i}")
    return out


def _shaper(**kw):
    return EagerToolOutputClearShaper(keep_recent=3, **kw)


def _tool_content(messages, tid):
    return next(m["content"] for m in messages if m.get("tool_call_id") == tid)


def _is_stub(content):
    return content.startswith(EAGER_STUB_PREFIX)


# --- always-on (no estimate gate) ---


def test_runs_with_no_trigger_clears_old_without_expanding_short_results():
    # The eager rung is not estimate-gated, but it still preserves its non-growth
    # invariant: old one-byte results remain smaller than the explanatory stub.
    messages = [_sys()]
    for i in range(1, 6):
        messages += _exchange(f"t{i}", body="s")  # 1-char bodies
    out = _shaper().shape(messages)
    assert _tool_content(out, "t1") == "s"
    assert _tool_content(out, "t2") == "s"
    # last 3 verbatim
    assert _tool_content(out, "t3") == "s"
    assert _tool_content(out, "t5") == "s"


def test_reused_call_id_only_clears_the_old_occurrence():
    messages = [
        _sys(),
        *_exchange("call_1", body="old" * 300),
        *_exchange("call_1", body="new result"),
    ]
    out = EagerToolOutputClearShaper(keep_recent=1).shape(messages)
    results = [message["content"] for message in out if message.get("role") == "tool"]
    assert _is_stub(results[0])
    assert results[1] == "new result"


def test_keeps_recent_k_verbatim_stubs_older():
    out = _shaper().shape(_history(7))  # keep_recent=3
    stubbed = {
        m["tool_call_id"]
        for m in out
        if m.get("role") == "tool" and _is_stub(m["content"])
    }
    verbatim = {
        m["tool_call_id"]
        for m in out
        if m.get("role") == "tool" and not _is_stub(m["content"])
    }
    assert stubbed == {"t1", "t2", "t3", "t4"}
    assert verbatim == {"t5", "t6", "t7"}


def test_noop_when_within_keep_recent():
    # exactly keep_recent compactable results -> nothing older -> identity
    msgs = _history(3)
    assert _shaper().shape(msgs) is msgs


@pytest.mark.parametrize("keep_recent", [0, -1, True, 1.5])
def test_rejects_invalid_keep_recent(keep_recent):
    with pytest.raises(ValueError, match="keep_recent"):
        EagerToolOutputClearShaper(keep_recent=keep_recent)


# --- DETERMINISTIC ---


def test_deterministic_byte_identical_across_calls():
    messages = _history(8)
    first = _shaper().shape(messages)
    second = _shaper().shape(copy.deepcopy(messages))
    assert first == second
    # stub text is a pure function of the message, not a counter/uuid
    assert _tool_content(first, "t1") == _tool_content(second, "t1")


def test_clear_decision_never_expands_an_aged_result():
    # Stub text is deterministic, but an old result is replaced only when doing
    # so actually shrinks that result.
    small = [_sys(), *_exchange("a", body="x"), *_exchange("b", body="x"),
             *_exchange("c", body="x"), *_exchange("d", body="x")]
    big = [_sys(), *_exchange("a", body="x" * 9000),
           *_exchange("b", body="x" * 9000), *_exchange("c", body="x" * 9000),
           *_exchange("d", body="x" * 9000)]
    sh = EagerToolOutputClearShaper(keep_recent=3)
    assert _tool_content(sh.shape(small), "a") == "x"
    assert _is_stub(_tool_content(sh.shape(big), "a"))


def test_stub_names_tool_and_target():
    messages = [
        _sys(),
        *_exchange("r", name="file_read",
                   arguments='{"path": "src/foo.py"}'),
        *_exchange("a"), *_exchange("b"), *_exchange("c"),
    ]
    out = EagerToolOutputClearShaper(keep_recent=3).shape(messages)
    stub = _tool_content(out, "r")
    assert "file_read" in stub and "src/foo.py" in stub


def test_default_keep_recent_retains_more_than_legacy_five():
    # Regression: keep_recent=5 made a multi-file task (working set > 5 reads)
    # thrash — it could never hold all its files at once, so it re-read them in a
    # loop until the step cap. The default now retains 12, so a ~13-read recon
    # keeps all but the single oldest verbatim.
    out = EagerToolOutputClearShaper().shape(_history(13))  # DEFAULT keep_recent
    stubbed = {
        m["tool_call_id"]
        for m in out
        if m.get("role") == "tool" and _is_stub(m["content"])
    }
    assert stubbed == {"t1"}  # only the oldest (13 - 12)
    assert not _is_stub(_tool_content(out, "t6"))  # was stubbed under the old K=5


def test_stub_includes_read_range_so_pages_get_distinct_stubs():
    # Regression: each page of a multi-page read must yield a DISTINCT stub naming
    # its line range, so a cleared paged read tells the model exactly which slice
    # it already holds (and need not re-read). Identical stubs per file caused the
    # model to re-read pages it had already seen.
    messages = [
        _sys(),
        *_exchange("p1", name="file_read",
                   arguments='{"path": "a.py", "offset": 1, "limit": 100}'),
        *_exchange("p2", name="file_read",
                   arguments='{"path": "a.py", "offset": 101, "limit": 100}'),
        *_exchange("x"), *_exchange("y"), *_exchange("z"),
    ]
    out = EagerToolOutputClearShaper(keep_recent=3).shape(messages)
    s1 = _tool_content(out, "p1")
    s2 = _tool_content(out, "p2")
    assert "a.py lines 1-100" in s1
    assert "a.py lines 101-200" in s2
    assert s1 != s2
    assert "already ran this" in s1  # wording discourages a re-read-to-reconfirm


# --- MONOTONIC ---


def test_monotonic_one_more_exchange_flips_exactly_one_and_keeps_old_stubs():
    sh = EagerToolOutputClearShaper(keep_recent=3)
    turn_n = sh.shape(_history(6))  # stubs t1,t2,t3 ; verbatim t4,t5,t6
    turn_n1 = sh.shape(_history(7))  # adds t7 ; t4 should now flip to stub

    # t4 transitions full -> stub exactly once as it ages past K.
    assert not _is_stub(_tool_content(turn_n, "t4"))
    assert _is_stub(_tool_content(turn_n1, "t4"))

    # Every already-stubbed message stays byte-identical (cacheable prefix).
    for tid in ("t1", "t2", "t3"):
        assert _tool_content(turn_n, tid) == _tool_content(turn_n1, tid)
        assert _is_stub(_tool_content(turn_n1, tid))


def test_monotonic_never_unstubs():
    sh = EagerToolOutputClearShaper(keep_recent=3)
    once = sh.shape(_history(8))
    # feeding an already-shaped view back never restores a body
    again = sh.shape(once)
    for tid in ("t1", "t2", "t3", "t4", "t5"):
        assert _is_stub(_tool_content(again, tid))


# --- IDEMPOTENT ---


def test_idempotent_shape_of_shape_equals_shape():
    messages = _history(9)
    once = _shaper().shape(messages)
    twice = _shaper().shape(once)
    assert twice == once
    # second pass finds nothing to change -> returns the same object
    assert twice is once


# --- structural guarantees ---


def test_non_compactable_tools_untouched():
    messages = [
        _sys(),
        *_exchange("p1", name="apply_patch", body="p" * 500),
        *_exchange("p2", name="apply_patch", body="p" * 500),
        *_exchange("b1"), *_exchange("b2"), *_exchange("b3"), *_exchange("b4"),
    ]
    out = EagerToolOutputClearShaper(keep_recent=1).shape(messages)
    # apply_patch results never stubbed regardless of age.
    assert _tool_content(out, "p1") == "p" * 500
    assert _tool_content(out, "p2") == "p" * 500


def test_pinned_sources_never_touched():
    pinned_call = {
        **_call("pin", name="bash"),
        "_ctx": {"priority": PIN_FLOOR + 5},
    }
    pinned_result = {**_tool("pin", "secret" * 100), "_ctx": {"priority": PIN_FLOOR + 5}}
    messages = [
        _sys(),
        pinned_call, pinned_result,
        *_exchange("a"), *_exchange("b"), *_exchange("c"), *_exchange("d"),
    ]
    out = EagerToolOutputClearShaper(keep_recent=3).shape(messages)
    # The pinned result keeps its full body even though it is the oldest.
    assert _tool_content(out, "pin") == "secret" * 100


def test_preserves_tool_call_pairing_no_orphans():
    out = _shaper().shape(_history(8))
    assert _orphaned_tool_ids(out) == set()
    # every tool message still present (content cleared, skeleton kept)
    assert sum(1 for m in out if m.get("role") == "tool") == 8


def test_input_messages_not_mutated():
    messages = _history(8)
    snapshot = copy.deepcopy(messages)
    out = _shaper().shape(messages)
    assert out is not messages
    assert messages == snapshot  # persisted transcript view unchanged
