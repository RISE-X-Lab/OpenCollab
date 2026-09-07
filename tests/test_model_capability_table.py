"""What the exact-model capability table is allowed to say about each model.

The table is a silent switch: an unlisted model gets the dataclass defaults and
the run proceeds with a different compaction threshold and a different tool
choice than a listed one, with nothing in the log saying so. These tests pin
the two ends of that switch — the entry that was measured, and the entries that
must not move because data was already collected under them.
"""

from __future__ import annotations

import dataclasses

from opencollab.adapters.llm.types import ModelCapabilities, model_capabilities
from opencollab.application.shaping.pipeline import history_trigger_target

# Measured against the DashScope OpenAI-compatible endpoint on 2026-09-06 and
# recorded in ``_EXACT_MODEL_CAPABILITIES``. ``context_window`` is the largest
# *input* the model accepts in the mode this adapter runs it in (thinking, via
# ``reasoning_effort=max``): 983,616. The endpoint publishes three other numbers
# for this model — 1,000,000 total context, 991,808 max input with thinking off,
# and 131,072 max *output* — and none of them belongs in this field.
# ``supports_forced_tool_choice`` is False because the endpoint answers HTTP 400
# to both a ``required`` and a named ``tool_choice`` in the mode this adapter
# sends.
QWEN_MEASURED_CONTEXT_WINDOW = 983_616
QWEN_MAX_OUTPUT_TOKENS = 131_072
QWEN_MEASURED_FORCED_TOOL_CHOICE = False


def test_qwen_flash_carries_the_measured_context_window():
    assert model_capabilities("qwen3.8-flash").context_window == QWEN_MEASURED_CONTEXT_WINDOW


def test_qwen_flash_context_window_is_not_the_max_output_limit():
    """The two limits are different quantities and the endpoint words its 400s
    differently ("Range of max_tokens" against "Range of input length"). Filing
    the output limit under ``context_window`` is the exact regression this
    pins: it silently moved compaction from 950,616 tokens down to 98,072.
    """
    assert model_capabilities("qwen3.8-flash").context_window != QWEN_MAX_OUTPUT_TOKENS


def test_qwen_flash_window_leaves_the_designed_headroom_under_the_real_limit():
    """``history_trigger_target`` reserves 33,000 tokens below the window.

    Recording the 1,000,000-token *total context* instead would put the trigger
    at 967,000, i.e. 16,616 below the real 983,616 input ceiling — under half
    the reserve. Spelled out as literals so the test fails if either the window
    or the reserve moves.
    """
    window = model_capabilities("qwen3.8-flash").context_window

    assert history_trigger_target(window) == (950_616, 712_962)


def test_qwen_flash_refuses_forced_tool_choice():
    capabilities = model_capabilities("qwen3.8-flash")

    assert capabilities.supports_forced_tool_choice is QWEN_MEASURED_FORCED_TOOL_CHOICE


def test_qwen_flash_leaves_every_unprobed_dimension_at_its_default():
    """Four dimensions were never probed, so the entry must not assert them.

    Copying them from the neighbouring ``deepseek-v4-flash`` row would read as
    measurement and be invention; this fails if anyone does.
    """
    capabilities = model_capabilities("qwen3.8-flash")
    default = ModelCapabilities()

    for field in (
        "supports_responses_json_schema",
        "honors_workflow_thinking_override",
        "supports_responses_reasoning",
        "supports_responses_tools",
    ):
        assert getattr(capabilities, field) == getattr(default, field), field


def test_adding_qwen_leaves_deepseek_byte_for_byte_unchanged():
    """The deepseek arm's 240 recorded runs must still resolve to this row.

    Every field is spelled out rather than compared to the live table, so the
    test fails if the row is edited as well as if the lookup is.
    """
    assert dataclasses.asdict(model_capabilities("deepseek-v4-flash")) == {
        "context_window": 1_048_576,
        "supports_forced_tool_choice": False,
        "supports_responses_json_schema": True,
        "honors_workflow_thinking_override": False,
        "supports_responses_streaming": True,
        "supports_responses_sampling": True,
        "supports_responses_reasoning": True,
        "supports_responses_tools": True,
    }


def test_luna_is_still_unlisted_and_still_gets_the_fallback():
    """``gpt-5.6-luna`` has no row on purpose; its runs were collected without one.

    The values below are what an unlisted ``gpt-5``-family identifier falls back
    to. Giving luna a row would change them, and would change the instrument the
    already-collected luna runs were produced on.
    """
    assert dataclasses.asdict(model_capabilities("gpt-5.6-luna")) == {
        "context_window": None,
        "supports_forced_tool_choice": True,
        "supports_responses_json_schema": False,
        "honors_workflow_thinking_override": True,
        "supports_responses_streaming": True,
        "supports_responses_sampling": True,
        "supports_responses_reasoning": True,
        "supports_responses_tools": True,
    }
