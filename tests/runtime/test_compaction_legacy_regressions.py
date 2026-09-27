"""Legacy context repairs retain the current pressure-driven history policy."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from opencollab.application.shaping import AutoCompactShaper
from opencollab.bootstrap.container import _build_default_shaper


def test_small_window_retains_the_latest_location_and_evidence_pair():
    pipeline = _build_default_shaper(SimpleNamespace(context_window=lambda: 35_500), lambda _: "summary")
    clear, snip, compact = pipeline._shapers[1:]
    assert clear.keep_recent == 2
    assert snip.keep_recent_groups == compact.keep_recent_groups == 2


def test_large_window_keeps_default_history_and_does_not_clear_early():
    pipeline = _build_default_shaper(SimpleNamespace(context_window=lambda: 1_048_576), lambda _: "summary")
    clear, snip, compact = pipeline._shapers[1:]
    assert clear.keep_recent == 5
    assert snip.keep_recent_groups == compact.keep_recent_groups == 4
    messages = [{"role": "user", "content": "task"}]
    for index in range(20):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": str(index), "function": {"name": "file_read", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "source code" * 30},
            ]
        )
    assert pipeline.shape(messages) == messages


def test_an_oversized_summary_keeps_original_history_without_growth():
    messages = [{"role": "system", "content": "instructions"}]
    for index in range(5):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": str(index), "function": {"name": "file_read", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "source code" * 300},
            ]
        )
    original = copy.deepcopy(messages)
    shaper = AutoCompactShaper(
        summarizer=lambda _: "summary" * 20_000, trigger_tokens=1000, target_tokens=500, keep_recent_groups=1
    )
    assert shaper.shape(messages) == original
    assert messages == original
