"""STEP 1 — information-gain sensor.

Classify each EXECUTED tool result as informative vs low-yield and maintain the
novelty counters on ``SessionState``. STEP 1 adds NO braking — the counters are
purely observational; later steps key brakes on them.

* T1 (SENSOR) — duplicate / empty / "No matches"-class results increment
  ``low_yield_since_progress``; a NOVEL informative result resets it AND
  increments ``distinct_evidence_count``. Includes the red-team re-read case (a
  path-normalized re-read of a known file at a shifted range yields novel content
  but is still scored zero-gain via the call hash).
* T2 (off==on parity) — folding the sensor (``apply_to``, the "on" path) leaves
  every CONTROL-FLOW-visible piece of state byte-for-byte identical to NOT folding
  it (the pre-sensor "off"/reference apply body); only the new observational
  counters move.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import (
    ToolExecutionUseCase,
    _intrinsic_low_yield,
    _result_content_hash,
)
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


class FakeAgent:
    def __init__(self, tools=None):
        self.tools = tools or []

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class FakeEventPublisher:
    async def emit(self, event):  # pragma: no cover - trivial sink
        pass


class ScriptedTool:
    """A tool returning a queued list of outputs (so one path can yield differing
    content across calls), recording the args it was invoked with."""

    def __init__(self, name, outputs):
        self.name = name
        self._outputs = list(outputs)
        self.calls = []

    async def execute_with_runtime(self, args, runtime):
        self.calls.append(args)
        return self._outputs.pop(0) if self._outputs else ""


def _event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(type="loop_detected", data={}),
        tool_start=lambda tool, args: SimpleNamespace(type="tool_start", data={}),
        tool_end=lambda tool, latency: SimpleNamespace(type="tool_end", data={}),
    )


def _use_case(state, tool):
    return ToolExecutionUseCase(
        agent=FakeAgent(tools=[tool]),
        environment=None,
        state=state,
        event_publisher=FakeEventPublisher(),
        event_factory=_event_factory(),
    )


def _call(name, args, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args}}


def _run_one(state, tool, name, args):
    """Process a single tool call and fold the result into ``state``."""
    result = run(_use_case(state, tool).process([_call(name, args)]))
    result.apply_to(state)
    return result


# --------------------------------------------------------------------------- #
# Unit: the two stateless classifier inputs.
# --------------------------------------------------------------------------- #


def test_intrinsic_low_yield_flags_no_match_empty_read_and_zero_lines():
    assert _intrinsic_low_yield("grep", "No matches found for pattern: zzz") is True
    assert _intrinsic_low_yield("file_read", "") is True
    assert _intrinsic_low_yield("file_read", "   \n  ") is True
    assert _intrinsic_low_yield("file_read", "File: e.py (0 lines total, showing 1-0)") is True
    # A real read hit and a real grep hit are NOT intrinsically low-yield.
    assert _intrinsic_low_yield("file_read", "File: a.py (3 lines)\n1\tdef f(): ...") is False
    assert _intrinsic_low_yield("grep", "a.py:1: def f(): ...") is False
    # An empty bash result is not inherently low-yield (only read-class tools).
    assert _intrinsic_low_yield("bash", "") is False


def test_content_hash_is_stable_and_distinguishes_content():
    assert _result_content_hash("alpha") == _result_content_hash("alpha")
    assert _result_content_hash("alpha") != _result_content_hash("beta")


# --------------------------------------------------------------------------- #
# T1 — SENSOR: low-yield increments, novel resets + counts.
# --------------------------------------------------------------------------- #


def test_t1_low_yield_increments_and_novel_resets_and_counts():
    state = SessionState(messages=[])

    # 1) A novel grep hit -> informative: low_yield stays 0, distinct = 1.
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"end"}')
    assert state.distinct_evidence_count == 1
    assert state.low_yield_since_progress == 0

    # 2) Exact CONTENT duplicate (different args, identical returned content) ->
    #    low-yield, no new evidence. (call hash differs; content hash collides.)
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"start"}')
    assert state.distinct_evidence_count == 1
    assert state.low_yield_since_progress == 1

    # 3) An empty read -> low-yield.
    _run_one(state, ScriptedTool("file_read", [""]), "file_read", '{"path":"empty.py"}')
    assert state.low_yield_since_progress == 2

    # 4) A "No matches found" grep -> low-yield (the no-match class, first seen).
    _run_one(state, ScriptedTool("grep", ["No matches found for pattern: zzz"]), "grep", '{"pattern":"zzz"}')
    assert state.low_yield_since_progress == 3

    # 5) A NOVEL informative result RESETS low_yield and increments distinct.
    _run_one(
        state,
        ScriptedTool("file_read", ["File: b.py (2 lines total)\n1\tdef f(): pass"]),
        "file_read",
        '{"path":"b.py"}',
    )
    assert state.low_yield_since_progress == 0
    assert state.distinct_evidence_count == 2


def test_t1_path_normalized_reread_scores_zero_gain_even_with_new_content():
    # Red-team: re-cat a KNOWN file at a shifted range returns DIFFERENT content
    # (novel content hash) but the SAME path-normalized (tool, args) call key, so
    # the sensor must still score it zero-gain (low-yield) — a model cannot dodge
    # the sensor by re-reading the same file with a different line range.
    state = SessionState(messages=[])
    _run_one(
        state,
        ScriptedTool("file_read", ["File: ccode.py (...)\n1\talpha"]),
        "file_read",
        '{"path":"ccode.py","offset":1}',
    )
    assert state.distinct_evidence_count == 1
    assert state.low_yield_since_progress == 0

    _run_one(
        state,
        ScriptedTool("file_read", ["File: ccode.py (...)\n50\tbeta"]),  # new content, same file
        "file_read",
        '{"path":"ccode.py","offset":50}',
    )
    assert state.distinct_evidence_count == 1  # NOT counted as new evidence
    assert state.low_yield_since_progress == 1


def test_t1_within_batch_duplicate_is_low_yield():
    # Two calls in ONE batch returning identical content: the first is informative,
    # the second collides on the content hash within the same apply -> low-yield.
    state = SessionState(messages=[])
    tool = ScriptedTool("grep", ["hit X", "hit X"])
    batch = [_call("grep", '{"pattern":"a"}', cid="c1"), _call("grep", '{"pattern":"b"}', cid="c2")]
    run(_use_case(state, tool).process(batch)).apply_to(state)
    assert state.distinct_evidence_count == 1
    assert state.low_yield_since_progress == 1


def test_t1_counters_reset_on_a_fresh_user_turn():
    state = SessionState(messages=[])
    _run_one(state, ScriptedTool("grep", ["a hit"]), "grep", '{"pattern":"a"}')
    _run_one(state, ScriptedTool("grep", ["No matches found for pattern: z"]), "grep", '{"pattern":"z"}')
    assert state.distinct_evidence_count == 1 and state.low_yield_since_progress == 1

    state.reset_for_user_turn()
    assert state.low_yield_since_progress == 0
    assert state.distinct_evidence_count == 0
    assert state._seen_result_hashes == set()


# --------------------------------------------------------------------------- #
# T2 — off == on parity: the sensor is observational, control flow unchanged.
# --------------------------------------------------------------------------- #


def test_t2_sensor_folding_does_not_alter_control_flow_off_equals_on():
    # The ONLY thing the sensor adds to ``apply_to`` is the evidence fold. Folding
    # it ("on") must leave every control-flow-visible piece of state byte-for-byte
    # identical to the pre-sensor apply body ("off"/reference); only the new
    # observational counters move.
    batch = [_call("grep", '{"pattern":"end"}')]

    # Reference ("off") = the pre-sensor apply body: append messages + hashes +
    # read/write counter, WITHOUT folding the evidence sensor.
    ref = SessionState(messages=[])
    res_ref = run(_use_case(ref, ScriptedTool("grep", ["fs.py:42: end = start + n"])).process(batch))
    for message in res_ref.messages_to_append:
        ref.append_message(message)
    res_ref.apply_hashes_to(ref)
    res_ref.apply_read_write_counter_to(ref)

    # Enforced ("on") = full apply_to, which additionally folds the sensor.
    on = SessionState(messages=[])
    res_on = run(_use_case(on, ScriptedTool("grep", ["fs.py:42: end = start + n"])).process(batch))
    res_on.apply_to(on)

    # Control-flow-visible state is identical between off and on.
    assert on.messages == ref.messages
    assert on.reads_since_last_edit == ref.reads_since_last_edit
    assert on.recent_call_hashes == ref.recent_call_hashes
    assert res_on.messages_to_append == res_ref.messages_to_append
    assert res_on.loop_detections == res_ref.loop_detections
    assert res_on.reads_executed == res_ref.reads_executed

    # Only the observational counters diverge: "off" never folded the sensor.
    assert ref.distinct_evidence_count == 0 and ref.low_yield_since_progress == 0
    assert on.distinct_evidence_count == 1 and on.low_yield_since_progress == 0
