"""Unit tests for the shaper pipeline and per-tool-result budget shaper."""

from __future__ import annotations

import copy

from opencollab.application.shaping import (
    COMPACTED_MARKER_PREFIX,
    AutoCompactShaper,
    ContextCollapseShaper,
    OldHistorySnipShaper,
    PerToolResultBudgetShaper,
    ShaperPipeline,
)


def _tool_msg(content, tool_call_id="t1"):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def test_oversize_tool_result_is_truncated_to_budget_with_reference():
    shaper = PerToolResultBudgetShaper(max_chars=1000)
    big = "x" * 5000
    out = shaper.shape([_tool_msg(big)])
    content = out[0]["content"]
    assert len(content) <= 1000
    assert content.startswith("x" * 800)
    assert "re-read a narrower range" in content
    assert "truncated" in content


def test_under_budget_tool_result_is_untouched():
    shaper = PerToolResultBudgetShaper(max_chars=1000)
    small = "x" * 500
    out = shaper.shape([_tool_msg(small)])
    assert out[0]["content"] == small


def test_non_tool_messages_pass_through_even_when_huge():
    shaper = PerToolResultBudgetShaper(max_chars=100)
    user = {"role": "user", "content": "u" * 5000}
    assistant = {"role": "assistant", "content": "a" * 5000}
    out = shaper.shape([user, assistant])
    assert out == [user, assistant]


def test_shaper_does_not_mutate_input():
    shaper = PerToolResultBudgetShaper(max_chars=1000)
    messages = [_tool_msg("y" * 5000)]
    snapshot = copy.deepcopy(messages)
    shaper.shape(messages)
    assert messages == snapshot


def test_any_size_result_fits_budget():
    shaper = PerToolResultBudgetShaper(max_chars=2000)
    for n in (1999, 2000, 2001, 100_000):
        out = shaper.shape([_tool_msg("z" * n)])
        assert len(out[0]["content"]) <= 2000


def test_empty_pipeline_is_identity():
    messages = [_tool_msg("z" * 100_000)]
    out = ShaperPipeline(()).shape(messages)
    assert out is messages


def test_pipeline_applies_shapers_in_order():
    class TagShaper:
        def __init__(self, tag):
            self.tag = tag

        def shape(self, messages):
            return [{**m, "tags": [*m.get("tags", []), self.tag]} for m in messages]

    pipeline = ShaperPipeline((TagShaper("a"), TagShaper("b")))
    out = pipeline.shape([{"role": "user", "content": "x"}])
    assert out[0]["tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# History-compaction layers (snip / auto-compact / collapse)
# ---------------------------------------------------------------------------


def _chars(messages):
    """Char-counting estimator — additive and predictable for thresholds."""
    return sum(len(m.get("content") or "") for m in messages)


def _sys():
    return {"role": "system", "content": "s"}


def _user(c="u"):
    return {"role": "user", "content": c}


def _call(tid, text=""):
    msg = {
        "role": "assistant",
        "tool_calls": [{"id": tid, "function": {"name": "bash", "arguments": "{}"}}],
    }
    if text:
        msg["content"] = text
    return msg


def _tool(tid, c):
    return {"role": "tool", "tool_call_id": tid, "content": c}


def _text(c):
    return {"role": "assistant", "content": c}


def _orphaned_tool_ids(messages):
    """tool-result ids that lack a surviving assistant tool_call answering them."""
    call_ids = {
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls", [])
    }
    result_ids = {
        m["tool_call_id"] for m in messages if m.get("role") == "tool"
    }
    return result_ids - call_ids


def _snip(**kw):
    return OldHistorySnipShaper(
        estimate_tokens=_chars, trigger_tokens=1500, target_tokens=800,
        keep_recent_groups=1, **kw,
    )


def _autocompact(summarizer, **kw):
    return AutoCompactShaper(
        summarizer=summarizer, estimate_tokens=_chars, trigger_tokens=1500,
        target_tokens=800, keep_recent_groups=1, **kw,
    )


def test_snip_noop_below_trigger_returns_input_identity():
    messages = [_sys(), _user(), _text("small"), _text("recent")]
    out = _snip().shape(messages)
    assert out is messages


def test_snip_drops_old_tool_turns_when_over_trigger():
    messages = [
        _sys(),
        _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2"), _tool("t2", "x" * 1000),
        _text("recent answer"),
    ]
    out = _snip().shape(messages)
    # Old tool-exchange turns are gone; system, user and recent group survive.
    assert not any(m.get("role") == "tool" for m in out)
    assert not any(m.get("tool_calls") for m in out)
    assert out[0] == _sys()
    assert out[-1] == _text("recent answer")
    assert _user() in out


def test_snip_brings_view_under_target_anti_thrash_headroom():
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2"), _tool("t2", "x" * 1000),
        _text("recent"),
    ]
    out = _snip().shape(messages)
    # Compacted to <= target (well under trigger) so next turn won't immediately
    # re-trigger.
    assert _chars(out) <= 800


def test_snip_preserves_tool_pairing():
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2", text="kept reasoning"), _tool("t2", "x" * 1000),
        _text("recent"),
    ]
    out = _snip().shape(messages)
    assert _orphaned_tool_ids(out) == set()


def test_snip_does_not_mutate_input():
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2"), _tool("t2", "x" * 1000),
        _text("recent"),
    ]
    snapshot = copy.deepcopy(messages)
    _snip().shape(messages)
    assert messages == snapshot


def test_autocompact_disabled_when_no_summarizer():
    messages = [
        _sys(), _user(),
        _text("x" * 1000), _text("y" * 1000),
        _text("recent"),
    ]
    out = _autocompact(summarizer=None).shape(messages)
    assert out is messages  # default-off switch: identity even over trigger


def test_autocompact_replaces_old_span_with_visible_marker():
    messages = [
        _sys(), _user(),
        _text("x" * 1000), _text("y" * 1000),
        _text("recent"),
    ]
    out = _autocompact(summarizer=lambda seg: "SUMMARY").shape(messages)
    markers = [m for m in out if str(m.get("content", "")).startswith(COMPACTED_MARKER_PREFIX)]
    assert len(markers) == 1
    marker = markers[0]
    assert marker["role"] == "system"
    assert marker["compacted"] is True
    assert "SUMMARY" in marker["content"]
    # System + recent survive verbatim; the old span collapsed to one marker.
    assert out[0] == _sys()
    assert out[-1] == _text("recent")


def test_autocompact_preserves_tool_pairing_at_group_boundary():
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2"), _tool("t2", "y" * 1000),
        _text("recent"),
    ]
    out = _autocompact(summarizer=lambda seg: "SUMMARY").shape(messages)
    assert _orphaned_tool_ids(out) == set()


def test_autocompact_does_not_mutate_input():
    messages = [_sys(), _user(), _text("x" * 1000), _text("y" * 1000), _text("recent")]
    snapshot = copy.deepcopy(messages)
    _autocompact(summarizer=lambda seg: "SUMMARY").shape(messages)
    assert messages == snapshot


def test_lazy_degradation_snip_sufficient_skips_autocompact():
    # Old tool turns are snippable, so snip alone gets under the trigger and the
    # downstream auto-compact sees no pressure → no summary marker appears.
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _call("t2"), _tool("t2", "x" * 1000),
        _text("recent"),
    ]
    pipeline = ShaperPipeline((_snip(), _autocompact(summarizer=lambda seg: "SUMMARY")))
    out = pipeline.shape(messages)
    assert not any(str(m.get("content", "")).startswith(COMPACTED_MARKER_PREFIX) for m in out)


def test_lazy_degradation_snip_insufficient_triggers_autocompact():
    # One snippable tool turn plus two valuable text turns snip won't touch:
    # snip fires but stays over trigger, so auto-compact finishes the job.
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 1000),
        _text("y" * 1000), _text("z" * 1000),
        _text("recent"),
    ]
    pipeline = ShaperPipeline((_snip(), _autocompact(summarizer=lambda seg: "SUMMARY")))
    out = pipeline.shape(messages)
    # snip acted (the tool turn is gone) AND auto-compact acted (marker present).
    assert not any(m.get("role") == "tool" for m in out)
    assert any(str(m.get("content", "")).startswith(COMPACTED_MARKER_PREFIX) for m in out)
    assert _orphaned_tool_ids(out) == set()


def test_context_collapse_is_identity_placeholder():
    messages = [_sys(), _user(), _text("a"), _text("b")]
    assert ContextCollapseShaper().shape(messages) is messages
