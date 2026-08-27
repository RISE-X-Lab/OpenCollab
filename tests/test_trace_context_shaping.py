"""Every model call must record which compaction rung actually fired.

The shaping pipeline reshapes a *copy* of the history, so the transcript keeps
the full content and nothing on disk says which of the five rungs ran on a given
turn. These tests pin a ``context_shaping`` trajectory record per turn:
``{seq, aid, rung, tokens_before, tokens_after}``, with ``rung`` drawn from a
closed six-value vocabulary. They drive the real
:class:`~opencollab.adapters.trace.Tracer` and read the JSONL back off disk, so
a field that never reaches the file fails here.
"""

from __future__ import annotations

import json

from session_run_loop_test_support import FakeLLM, build_runner, llm_response, run

from opencollab.adapters.trace import Tracer
from opencollab.application.shaping import (
    AutoCompactShaper,
    EagerToolOutputClearShaper,
    OldHistorySnipShaper,
    PerToolResultBudgetShaper,
    ShaperPipeline,
    ToolOutputClearShaper,
)
from opencollab.domain.session import SessionState

SESSION_AID = 11
STEP_COUNT = 4

# The frozen vocabulary the paper's ``assigned.context_policy`` field uses; a
# per-turn record outside it cannot be joined against the assigned policy.
RUNG_VOCABULARY = {
    "eager_clear",
    "per_tool_budget",
    "tool_output_clear",
    "old_history_snip",
    "auto_compact",
    "none",
}


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


def _tool_exchange(call_id: str, tool_name: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps({"path": f"/repo/{call_id}.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def _run_one_call(tmp_path, *, shaper, messages) -> list[dict]:
    """Run a single-turn session under a real Tracer; return shaping payloads."""
    tracer = Tracer(run_id="context-shaping", output_dir=str(tmp_path))
    path = tracer.path
    try:
        runner = build_runner(
            state=SessionState(
                messages=messages,
                aid=SESSION_AID,
                step_count=STEP_COUNT,
            ),
            llm=FakeLLM([llm_response(content="done")]),
            tracer=tracer,
            shaper=shaper,
        )
        assert run(runner.run_loop()) == "done"
        tracer.flush()
    finally:
        tracer.close()
    return _payloads(path, "context_shaping")


def test_one_rung_firing_is_recorded_by_its_frozen_name(tmp_path):
    """A single triggered rung yields one record naming it, with both sizes."""
    payloads = _run_one_call(
        tmp_path,
        shaper=ShaperPipeline((PerToolResultBudgetShaper(max_chars=64),)),
        messages=[
            {"role": "system", "content": "sys"},
            *_tool_exchange("call-1", "file_read", "X" * 5000),
        ],
    )

    assert len(payloads) == 1
    record = payloads[0]
    assert record["rung"] == "per_tool_budget"
    assert record["aid"] == SESSION_AID
    assert record["seq"] == STEP_COUNT
    assert record["tokens_before"] > record["tokens_after"]


def test_every_rung_that_fires_gets_its_own_record_in_order(tmp_path):
    """Two rungs firing in one turn yield two records, in pipeline order."""
    payloads = _run_one_call(
        tmp_path,
        shaper=ShaperPipeline(
            (
                EagerToolOutputClearShaper(keep_recent=1),
                PerToolResultBudgetShaper(max_chars=64),
            )
        ),
        messages=[
            {"role": "system", "content": "sys"},
            *_tool_exchange("call-1", "file_read", "A" * 5000),
            *_tool_exchange("call-2", "file_read", "B" * 5000),
        ],
    )

    assert [record["rung"] for record in payloads] == [
        "eager_clear",
        "per_tool_budget",
    ]
    for record in payloads:
        assert record["aid"] == SESSION_AID
        assert record["seq"] == STEP_COUNT
        assert record["tokens_before"] > record["tokens_after"]
    # The chain is sequential: each rung starts from the previous one's output.
    assert payloads[0]["tokens_after"] == payloads[1]["tokens_before"]


def test_a_turn_that_triggers_nothing_still_records_none(tmp_path):
    """No rung firing is recorded as ``none`` — not as an absent record."""
    payloads = _run_one_call(
        tmp_path,
        shaper=ShaperPipeline(
            (
                EagerToolOutputClearShaper(),
                PerToolResultBudgetShaper(),
                ToolOutputClearShaper(),
                OldHistorySnipShaper(),
                AutoCompactShaper(),
            )
        ),
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
    )

    assert len(payloads) == 1
    record = payloads[0]
    assert record["rung"] == "none"
    assert record["aid"] == SESSION_AID
    assert record["seq"] == STEP_COUNT
    assert record["tokens_before"] == record["tokens_after"] > 0


def test_recorded_rung_names_stay_inside_the_frozen_vocabulary(tmp_path):
    """Never a class name: the wired production chain reports frozen labels."""
    payloads = _run_one_call(
        tmp_path,
        shaper=ShaperPipeline(
            (
                EagerToolOutputClearShaper(keep_recent=1),
                PerToolResultBudgetShaper(max_chars=64),
                ToolOutputClearShaper(trigger_tokens=2, target_tokens=1, keep_recent=1),
                OldHistorySnipShaper(trigger_tokens=2, target_tokens=1),
            )
        ),
        messages=[
            {"role": "system", "content": "sys"},
            *_tool_exchange("call-1", "file_read", "A" * 5000),
            *_tool_exchange("call-2", "file_read", "B" * 5000),
            *_tool_exchange("call-3", "file_read", "C" * 5000),
            *_tool_exchange("call-4", "file_read", "D" * 5000),
            *_tool_exchange("call-5", "file_read", "E" * 5000),
            *_tool_exchange("call-6", "file_read", "F" * 5000),
            {"role": "user", "content": "carry on"},
        ],
    )

    assert payloads
    for record in payloads:
        assert record["rung"] in RUNG_VOCABULARY
        assert record["rung"] != "none"  # something did fire
