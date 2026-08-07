"""Unit tests for the clear-in-place tool-output shaper and trigger math."""

from __future__ import annotations

import copy

import pytest

from opencollab.application.shaping import (
    DEFAULT_CLEARED_TOOL_CONTENT,
    DEFAULT_HISTORY_TARGET_TOKENS,
    DEFAULT_HISTORY_TRIGGER_TOKENS,
    ToolOutputClearShaper,
    history_trigger_target,
)


def _chars(messages):
    return sum(len(m.get("content") or "") for m in messages)


def _sys():
    return {"role": "system", "content": "s"}


def _call(tid, name="bash"):
    return {
        "role": "assistant",
        "tool_calls": [{"id": tid, "function": {"name": name, "arguments": "{}"}}],
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


def _clearer(**kw):
    return ToolOutputClearShaper(
        estimate_tokens=_chars, trigger_tokens=1500, target_tokens=800,
        keep_recent=1, **kw,
    )


def _big_history():
    return [
        _sys(),
        _call("t1"), _tool("t1", "a" * 1000),
        _call("t2"), _tool("t2", "b" * 1000),
        _call("t3"), _tool("t3", "c" * 1000),
    ]


def test_noop_below_trigger_returns_identity():
    messages = [_sys(), _call("t1"), _tool("t1", "small")]
    out = _clearer().shape(messages)
    assert out is messages


def test_clears_old_compactable_results_keeping_recent_verbatim():
    out = _clearer().shape(_big_history())
    cleared = [m for m in out if m.get("role") == "tool" and m["content"] == DEFAULT_CLEARED_TOOL_CONTENT]
    intact = [m for m in out if m.get("role") == "tool" and m["content"] != DEFAULT_CLEARED_TOOL_CONTENT]
    # keep_recent=1 → only the last compactable result stays verbatim.
    assert {m["tool_call_id"] for m in cleared} == {"t1", "t2"}
    assert [m["tool_call_id"] for m in intact] == ["t3"]
    assert intact[0]["content"] == "c" * 1000


def test_non_compactable_tools_are_untouched():
    messages = [
        _sys(),
        _call("t1", name="apply_patch"), _tool("t1", "p" * 1000),
        _call("t2", name="bash"), _tool("t2", "b" * 1000),
        _call("t3", name="bash"), _tool("t3", "b" * 1000),
    ]
    out = _clearer().shape(messages)
    # apply_patch result is never a clear candidate, regardless of age.
    assert out[2]["content"] == "p" * 1000


def test_never_orphans_a_tool_call_id():
    out = _clearer().shape(_big_history())
    assert _orphaned_tool_ids(out) == set()
    # Skeleton intact: every tool result message still present.
    assert sum(1 for m in out if m.get("role") == "tool") == 3


def test_idempotent_reclearing_is_noop():
    once = _clearer().shape(_big_history())
    twice = _clearer().shape(once)
    assert twice is once  # nothing left to clear → identity


def test_immutable_new_list_when_changed_input_untouched():
    messages = _big_history()
    snapshot = copy.deepcopy(messages)
    out = _clearer().shape(messages)
    assert out is not messages
    assert messages == snapshot


def test_keeps_all_when_compactable_count_within_keep_recent():
    messages = [_sys(), _call("t1"), _tool("t1", "a" * 2000)]
    # Over trigger, but only one compactable result and keep_recent=1 → nothing cleared.
    out = _clearer().shape(messages)
    assert out is messages


# --- trigger math (Step 3) ---


def test_history_trigger_target_scales_with_window():
    trigger_big, target_big = history_trigger_target(200_000)
    trigger_small, target_small = history_trigger_target(64_000)
    assert trigger_big > trigger_small
    assert target_big > target_small
    # effective = window - reserve(20k) - buffer(13k); target = trigger * 0.75
    assert trigger_big == 200_000 - 20_000 - 13_000
    assert target_big == int(trigger_big * 0.75)


def test_history_trigger_target_defaults_when_window_unknown():
    for unknown in (None, 0, -5):
        assert history_trigger_target(unknown) == (
            DEFAULT_HISTORY_TRIGGER_TOKENS,
            DEFAULT_HISTORY_TARGET_TOKENS,
        )


def test_history_trigger_target_never_negative_for_tiny_window():
    trigger, target = history_trigger_target(1_000)
    assert 1 <= target < trigger


@pytest.mark.parametrize(
    "kwargs",
    [
        {"output_reserve": -1},
        {"output_reserve": True},
        {"buffer": -1},
        {"buffer": 1.5},
        {"target_ratio": 0},
        {"target_ratio": 1},
        {"target_ratio": float("nan")},
        {"target_ratio": float("inf")},
    ],
)
def test_history_trigger_target_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        history_trigger_target(200_000, **kwargs)


def test_model_context_window_lookup_matches_by_substring():
    from opencollab.adapters.llm import model_context_window

    assert model_context_window("claude-opus-4-8-2026") == 200_000
    assert model_context_window("gpt-4o-mini") == 128_000
    assert model_context_window("deepseek-chat") == 64_000
    assert model_context_window("glm-5.2") == 400_000
    assert model_context_window("k3") == 1_048_576
    assert model_context_window("kimi-for-coding") == 262_144
    for near_miss in ("k3-256k", "kimi-k3", "kimi-k2.6", "kimi-k2.70", "kimi-for-coding-preview"):
        assert model_context_window(near_miss) is None
    assert model_context_window("some-unknown-model") is None
    assert model_context_window(None) is None
