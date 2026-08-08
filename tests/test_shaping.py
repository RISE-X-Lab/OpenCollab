"""Unit tests for the shaper pipeline and per-tool-result budget shaper."""

from __future__ import annotations

import copy

import pytest

from opencollab.application.shaping import (
    COMPACTED_MARKER_PREFIX,
    DEFAULT_CLEARED_TOOL_CONTENT,
    PIN_FLOOR,
    AutoCompactShaper,
    OldHistorySnipShaper,
    PerToolResultBudgetShaper,
    ShaperPipeline,
    ToolOutputClearShaper,
)
from opencollab.application.shaping.pipeline import approx_messages_tokens


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


def test_reactive_clear_keeps_new_result_when_provider_reuses_call_id():
    messages = [
        _sys(),
        _call("call_1"),
        _tool("call_1", "old" * 300),
        _call("call_1"),
        _tool("call_1", "new result"),
        _text("recent"),
    ]
    shaper = ToolOutputClearShaper(
        estimate_tokens=_chars,
        trigger_tokens=2,
        target_tokens=1,
        keep_recent=1,
        keep_recent_groups=0,
    )
    out = shaper.shape(messages)
    results = [message["content"] for message in out if message.get("role") == "tool"]
    assert results == [DEFAULT_CLEARED_TOOL_CONTENT, "new result"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger_tokens": 0},
        {"trigger_tokens": True},
        {"target_tokens": 0},
        {"target_tokens": 1_500},
        {"keep_recent_groups": -1},
        {"keep_recent_groups": 1.5},
    ],
)
def test_reactive_shapers_reject_inconsistent_thresholds(kwargs):
    defaults = {
        "estimate_tokens": _chars,
        "trigger_tokens": 1_500,
        "target_tokens": 800,
        "keep_recent_groups": 1,
    }
    with pytest.raises(ValueError):
        OldHistorySnipShaper(**{**defaults, **kwargs})


@pytest.mark.parametrize("max_chars", [True, 0, -1, 1.5, float("nan")])
def test_tool_result_budget_rejects_invalid_limits(max_chars):
    with pytest.raises(ValueError, match="positive integer"):
        PerToolResultBudgetShaper(max_chars=max_chars)


@pytest.mark.parametrize("max_chars", [1, 5, 20, 100])
def test_tiny_tool_result_budgets_remain_hard_caps(max_chars):
    message = _tool_msg("z" * 1_000)
    snapshot = copy.deepcopy(message)
    content = PerToolResultBudgetShaper(max_chars=max_chars).shape([message])[0]["content"]
    assert 0 < len(content) <= max_chars
    assert message == snapshot


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


def test_snip_preserves_reasoning_only_tool_group_when_it_cannot_split_safely():
    """Provider-signed reasoning must not be detached from its tool call."""
    old_call = _call("large")
    old_call["reasoning_content"] = "r" * 6_000
    messages = [_sys(), _user(), old_call, _tool("large", "small result"), _text("recent")]
    shaper = OldHistorySnipShaper(
        estimate_tokens=approx_messages_tokens,
        trigger_tokens=1_000,
        target_tokens=500,
        keep_recent_groups=1,
    )

    out = shaper.shape(messages)

    assert approx_messages_tokens(messages) > shaper.trigger_tokens
    assert old_call in out
    assert _tool("large", "small result") in out
    assert out[-1] == _text("recent")


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


def test_snip_preserves_hybrid_assistant_text_when_dropping_tool_payload():
    hybrid = _call("t1", text="decision: keep the verified constraint")
    messages = [
        _sys(),
        _user(),
        hybrid,
        _tool("t1", "x" * 2_000),
        _text("recent"),
    ]
    out = _snip().shape(messages)
    preserved = next(
        message for message in out
        if message.get("content") == "decision: keep the verified constraint"
    )
    assert "tool_calls" not in preserved
    assert not any(message.get("role") == "tool" for message in out)


@pytest.mark.parametrize(
    "malformed_results",
    [
        [_tool("unknown", "x" * 2_000)],
        [_tool("t1", "x" * 1_000), _tool("t1", "duplicate")],
        [],
    ],
)
def test_snip_leaves_malformed_tool_exchanges_untouched(malformed_results):
    exchange = [_call("t1"), *malformed_results]
    messages = [_sys(), _user(), *exchange, _text("p" * 2_000), _text("recent")]
    out = _snip().shape(messages)
    assert all(message in out for message in exchange)


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


@pytest.mark.parametrize("summary_size", [1_000, 3_000])
def test_autocompact_rejects_summary_that_misses_target_or_grows_view(summary_size):
    class CountingSummarizer:
        cache_key = "oversized"
        last_call_cacheable = True

        def __init__(self):
            self.calls = 0

        def __call__(self, _segment):
            self.calls += 1
            return "s" * summary_size

    summarizer = CountingSummarizer()
    messages = [
        _sys(),
        _user(),
        _text("x" * 1_000),
        _text("y" * 1_000),
        _text("recent"),
    ]
    shaper = _autocompact(summarizer=summarizer)

    assert shaper.shape(messages) is messages
    assert shaper.shape(messages) is messages
    assert summarizer.calls == 1


def test_autocompact_reuses_summary_for_unchanged_segment():
    class CountingSummarizer:
        cache_key = "model-a:prompt-v1"
        last_call_cacheable = True

        def __init__(self):
            self.calls = 0

        def __call__(self, segment):
            self.calls += 1
            return f"SUMMARY-{self.calls}"

    summarizer = CountingSummarizer()
    shaper = _autocompact(summarizer=summarizer)
    messages = [_sys(), _user(), _text("x" * 1000), _text("y" * 1000), _text("recent")]

    first = shaper.shape(messages)
    second = shaper.shape(copy.deepcopy(messages))

    assert first == second
    assert summarizer.calls == 1


def test_autocompact_cache_invalidates_on_segment_or_summarizer_key_change():
    class CountingSummarizer:
        cache_key = "model-a:prompt-v1"
        last_call_cacheable = True

        def __init__(self):
            self.calls = 0

        def __call__(self, segment):
            self.calls += 1
            return f"SUMMARY-{self.calls}"

    summarizer = CountingSummarizer()
    shaper = _autocompact(summarizer=summarizer)
    messages = [_sys(), _user(), _text("x" * 1000), _text("y" * 1000), _text("recent")]

    shaper.shape(messages)
    changed = copy.deepcopy(messages)
    changed[2]["content"] += "changed"
    shaper.shape(changed)
    summarizer.cache_key = "model-b:prompt-v1"
    shaper.shape(changed)

    assert summarizer.calls == 3


def test_autocompact_reuses_summary_when_only_kept_tool_group_grows():
    class CountingSummarizer:
        cache_key = "model-a:prompt-v1"
        last_call_cacheable = True

        def __init__(self):
            self.calls = 0

        def __call__(self, segment):
            self.calls += 1
            return "SUMMARY"

    summarizer = CountingSummarizer()
    shaper = _autocompact(summarizer=summarizer)
    recent_call = {
        "role": "assistant",
        "tool_calls": [
            {"id": "r1", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "r2", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    messages = [
        _sys(),
        _user(),
        _text("x" * 1000),
        _text("y" * 1000),
        recent_call,
        _tool("r1", "first"),
    ]

    shaper.shape(messages)
    shaper.shape([*messages, _tool("r2", "second")])

    assert summarizer.calls == 1


def test_autocompact_summary_cache_is_bounded_lru():
    class CountingSummarizer:
        cache_key = "model-a:prompt-v1"
        last_call_cacheable = True

        def __init__(self):
            self.calls = 0

        def __call__(self, segment):
            self.calls += 1
            return f"SUMMARY-{self.calls}"

    summarizer = CountingSummarizer()
    shaper = _autocompact(summarizer=summarizer, summary_cache_size=1)
    first = [_sys(), _user(), _text("x" * 1000), _text("y" * 1000), _text("recent")]
    second = copy.deepcopy(first)
    second[2]["content"] += "changed"

    shaper.shape(first)
    shaper.shape(second)
    shaper.shape(first)

    assert summarizer.calls == 3


def test_autocompact_does_not_cache_fallback_or_failed_summary():
    class FlakySummarizer:
        cache_key = "model-a:prompt-v1"

        def __init__(self):
            self.calls = 0
            self.last_call_cacheable = False

        def __call__(self, segment):
            self.calls += 1
            self.last_call_cacheable = self.calls > 1
            return "fallback" if self.calls == 1 else "real summary"

    summarizer = FlakySummarizer()
    shaper = _autocompact(summarizer=summarizer)
    messages = [_sys(), _user(), _text("x" * 1000), _text("y" * 1000), _text("recent")]

    assert "fallback" in shaper.shape(messages)[1]["content"]
    assert "real summary" in shaper.shape(messages)[1]["content"]
    shaper.shape(messages)

    assert summarizer.calls == 2


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


# ---------------------------------------------------------------------------
# Layer-aware compaction: pinning (auto-compact never folds a pinned source)
# ---------------------------------------------------------------------------


def _ctx_user(content, layer="task", priority=80):
    return {"role": "user", "content": content, "_ctx": {"layer": layer, "priority": priority}}


def test_autocompact_never_folds_a_pinned_source_into_the_summary():
    # A pinned task sits in the droppable middle (group 1). Auto-compact must
    # summarize the non-pinned tool span around it but leave the task verbatim.
    task = _ctx_user("the immutable task", priority=PIN_FLOOR + 10)
    messages = [_sys(), task, _text("x" * 1000), _text("y" * 1000), _text("recent")]
    seen_segments = []

    def summarizer(segment):
        seen_segments.append(segment)
        return "SUMMARY"

    out = _autocompact(summarizer=summarizer).shape(messages)
    assert task in out                                   # pinned source survives
    assert any(str(m.get("content", "")).startswith(COMPACTED_MARKER_PREFIX) for m in out)
    # the task was never handed to the summarizer
    assert all(task not in segment for segment in seen_segments)


def test_autocompact_can_shed_low_priority_system_source_by_provenance():
    identity = {
        "role": "system",
        "content": "identity",
        "_ctx": {"name": "identity", "layer": "identity", "priority": 100},
    }
    team = {
        "role": "system",
        "content": "team",
        "_ctx": {"name": "team", "layer": "team", "priority": 90},
    }
    project = {
        "role": "system",
        "content": "project-map-" + "x" * 2_000,
        "_ctx": {"name": "project", "layer": "project", "priority": 30},
    }
    task = _ctx_user("task", priority=80)
    messages = [identity, team, project, task, _text("recent")]
    snapshot = copy.deepcopy(messages)

    out = _autocompact(summarizer=lambda _segment: "project context omitted").shape(messages)

    assert identity in out and team in out and task in out
    assert project not in out
    assert any(message.get("compacted") for message in out)
    assert messages == snapshot


# ---------------------------------------------------------------------------
# Forced maximal compaction (the context-overflow safety-net entry point)
# ---------------------------------------------------------------------------


def test_forced_shape_compacts_even_below_trigger():
    from opencollab.application.shaping import forced_shape

    # A view comfortably BELOW the trigger: the estimate-gated snip layer would
    # normally no-op. A forced pass must still drop the old tool turns.
    snip = OldHistorySnipShaper(
        estimate_tokens=_chars, trigger_tokens=1_000_000, target_tokens=10,
        keep_recent_groups=1,
    )
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 50),
        _call("t2"), _tool("t2", "x" * 50),
        _text("recent"),
    ]
    # Normal pass: identity (well below trigger).
    assert snip.shape(messages) is messages
    # Forced pass: old tool turns are dropped despite being under the trigger.
    out = forced_shape(snip, messages)
    assert not any(m.get("role") == "tool" for m in out)
    assert out[-1] == _text("recent")


def test_forced_shape_restores_forced_flag_after():
    from opencollab.application.shaping import forced_shape

    snip = OldHistorySnipShaper(
        estimate_tokens=_chars, trigger_tokens=1_000_000, target_tokens=10,
        keep_recent_groups=1,
    )
    messages = [_sys(), _user(), _call("t1"), _tool("t1", "x" * 50), _text("recent")]
    forced_shape(snip, messages)
    # The flag is restored, so a subsequent normal call no-ops again.
    assert snip._forced is False
    assert snip.shape(messages) is messages


def test_forced_shape_still_never_folds_pinned_source():
    from opencollab.application.shaping import forced_shape

    # Even under forced compaction, a pinned task is never handed to the
    # summarizer — the safety net must not destroy identity/team/task.
    task = _ctx_user("the immutable task", priority=PIN_FLOOR + 10)
    messages = [_sys(), task, _text("x" * 50), _text("y" * 50), _text("recent")]
    seen = []

    def summarizer(segment):
        seen.append(segment)
        return "SUMMARY"

    auto = AutoCompactShaper(
        summarizer=summarizer, estimate_tokens=_chars,
        trigger_tokens=1_000_000, target_tokens=10, keep_recent_groups=1,
    )
    out = forced_shape(auto, messages)
    assert task in out
    assert all(task not in segment for segment in seen)


def test_forced_shape_through_pipeline_reaches_nested_layers():
    from opencollab.application.shaping import forced_shape

    # A real pipeline wrapping a reactive layer: forcing the pipeline must reach
    # the nested layer (recursion through ShaperPipeline).
    snip = OldHistorySnipShaper(
        estimate_tokens=_chars, trigger_tokens=1_000_000, target_tokens=10,
        keep_recent_groups=1,
    )
    pipeline = ShaperPipeline((PerToolResultBudgetShaper(max_chars=10_000), snip))
    messages = [
        _sys(), _user(),
        _call("t1"), _tool("t1", "x" * 50),
        _text("recent"),
    ]
    assert pipeline.shape(messages) == messages  # normal: nothing to do
    out = forced_shape(pipeline, messages)
    assert not any(m.get("role") == "tool" for m in out)
