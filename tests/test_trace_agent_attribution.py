"""Trajectory records must name the agent that produced them.

``steering_nudge`` and ``commit_brake`` already stamp ``aid`` into their
payloads. These tests pin the same field onto the two high-volume record types
— ``llm_call`` and ``tool_exec`` — so a multi-agent trajectory file can be
attributed per agent after the fact. They drive the real
:class:`~opencollab.adapters.trace.Tracer` and read the JSONL back off disk, so
a field that never reaches the file fails here.
"""

from __future__ import annotations

import json

from session_run_loop_test_support import (
    FakeLLM,
    _steering_runner,
    build_runner,
    llm_response,
    run,
)
from tool_execution_test_support import FakeAgent, NullEventPublisher

from opencollab.adapters.trace import Tracer
from opencollab.application.events import default_session_event_factory
from opencollab.application.steering import READS_NUDGE_HARD
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionState

SESSION_AID = 7


class _RuntimeTool:
    name = "fake_tool"

    async def execute_with_runtime(self, args, runtime):
        return "runtime result"


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


def test_llm_call_trace_record_carries_session_aid(tmp_path):
    tracer = Tracer(run_id="attribution-llm", output_dir=str(tmp_path))
    path = tracer.path
    try:
        runner = build_runner(
            state=SessionState(
                messages=[{"role": "system", "content": "sys"}],
                aid=SESSION_AID,
            ),
            llm=FakeLLM([llm_response(content="done")]),
            tracer=tracer,
        )
        assert run(runner.run_loop()) == "done"
        tracer.flush()
    finally:
        tracer.close()

    payloads = _payloads(path, "llm_call")
    assert len(payloads) == 1
    assert payloads[0]["aid"] == SESSION_AID


def test_tool_exec_trace_record_carries_session_aid(tmp_path):
    tracer = Tracer(run_id="attribution-tool", output_dir=str(tmp_path))
    path = tracer.path
    try:
        use_case = ToolExecutionUseCase(
            agent=FakeAgent(tools=[_RuntimeTool()]),
            environment=None,
            state=SessionState(messages=[], aid=SESSION_AID),
            event_publisher=NullEventPublisher(),
            event_factory=default_session_event_factory(aid=SESSION_AID),
            tracer=tracer,
        )
        result = run(
            use_case.process(
                [
                    {
                        "id": "call-1",
                        "function": {"name": "fake_tool", "arguments": '{"value": 1}'},
                    }
                ]
            )
        )
        assert result.messages_to_append[0]["content"] == "runtime result"
        tracer.flush()
    finally:
        tracer.close()

    payloads = _payloads(path, "tool_exec")
    assert len(payloads) == 1
    assert payloads[0]["aid"] == SESSION_AID


def test_llm_call_aid_matches_the_steering_nudge_aid(tmp_path):
    """One agent's records must all join on the same ``aid`` value."""
    tracer = Tracer(run_id="attribution-join", output_dir=str(tmp_path))
    path = tracer.path
    try:
        runner, state = _steering_runner(READS_NUDGE_HARD, tracer=tracer, aid=SESSION_AID)
        assert run(runner.run_loop()) == "done"
        tracer.flush()
    finally:
        tracer.close()

    nudges = _payloads(path, "steering_nudge")
    calls = _payloads(path, "llm_call")
    assert len(nudges) == 1
    assert len(calls) == 1
    assert nudges[0]["aid"] == state.aid
    assert calls[0]["aid"] == nudges[0]["aid"]
