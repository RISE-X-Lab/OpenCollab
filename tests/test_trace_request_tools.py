"""Every ``llm_call`` record must name the tools the request actually carried.

``assigned.topology_nodes`` records the tool list each seat was *given* at
prebuild. It does not record what any individual request went out with, and the
two come apart: the steering hard rung narrows the outbound list to
``_WRITE_TOOLS``, which removes ``message_agent`` — the tool the delegation
measurement is built on — for that turn, and nothing on disk said so. These
tests pin the sent list (names only, sorted) and the sent ``tool_choice`` onto
the record that already exists for the call.
"""

from __future__ import annotations

import json

from session_run_loop_test_support import (
    FakeAgent,
    FakeLLM,
    FakeTracer,
    _agent_with_tool_schemas,
    build_runner,
    llm_response,
    run,
)

from opencollab.adapters.trace import Tracer
from opencollab.application.steering import READS_NUDGE_HARD
from opencollab.domain.session import SessionState, TurnEnforcementState


def _llm_payloads(tracer: FakeTracer) -> list[dict]:
    return [step["payload"] for step in tracer.steps if step["step_type"] == "llm_call"]


def test_llm_call_records_the_sent_tool_names_sorted():
    tracer = FakeTracer()
    runner = build_runner(
        tracer=tracer,
        llm=FakeLLM([llm_response(content="done")]),
        agent=_agent_with_tool_schemas("grep", "file_read", "message_agent"),
    )

    run(runner.run_loop())

    payloads = _llm_payloads(tracer)
    assert len(payloads) == 1
    assert payloads[0]["request_tool_names"] == ["file_read", "grep", "message_agent"]
    assert payloads[0]["request_tool_choice"] is None


def test_llm_call_records_the_hard_write_gate_dropping_message_agent():
    """The audit question this record exists to answer: was the tool table
    rewritten mid-run? At the hard read threshold it is — ``message_agent`` is
    not in the request the provider saw, and the trajectory now says that."""
    tracer = FakeTracer()
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    runner = build_runner(
        state=state,
        tracer=tracer,
        llm=FakeLLM([llm_response(content="done")]),
        agent=_agent_with_tool_schemas("file_read", "apply_patch", "message_agent"),
        max_budget_tokens=100_000,
        max_steps=40,
    )

    run(runner.run_loop())

    payloads = _llm_payloads(tracer)
    assert len(payloads) == 1
    assert payloads[0]["request_tool_names"] == ["apply_patch"]
    assert "message_agent" not in payloads[0]["request_tool_names"]
    assert payloads[0]["request_tool_choice"] == "required"


def test_llm_call_records_a_named_function_tool_choice_verbatim():
    tracer = FakeTracer()
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    runner = build_runner(
        state=state,
        tracer=tracer,
        llm=FakeLLM([llm_response(content="done")]),
        agent=_agent_with_tool_schemas("structured_output", "file_read", "grep"),
        max_budget_tokens=100_000,
        max_steps=40,
    )

    run(runner.run_loop())

    payloads = _llm_payloads(tracer)
    assert payloads[0]["request_tool_names"] == ["structured_output"]
    assert payloads[0]["request_tool_choice"] == {
        "type": "function",
        "function": {"name": "structured_output"},
    }


def test_llm_call_records_an_empty_list_when_no_tools_are_sent():
    tracer = FakeTracer()
    runner = build_runner(
        tracer=tracer,
        llm=FakeLLM([llm_response(content="done")]),
        agent=FakeAgent(),
    )

    run(runner.run_loop())

    assert _llm_payloads(tracer)[0]["request_tool_names"] == []


def test_the_record_carries_names_only_not_the_schemas(tmp_path):
    """Volume guard, checked on the real trajectory file: a few dozen bytes of
    names per call, not the parameter schemas that would multiply the file."""
    tracer = Tracer(run_id="request-tools", output_dir=str(tmp_path))
    path = tracer.path
    try:
        runner = build_runner(
            tracer=tracer,
            llm=FakeLLM([llm_response(content="done")]),
            agent=_agent_with_tool_schemas("grep", "file_read"),
        )
        assert run(runner.run_loop()) == "done"
        tracer.flush()
    finally:
        tracer.close()

    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    payloads = [r["payload"] for r in records if r["type"] == "llm_call"]
    assert len(payloads) == 1
    names = payloads[0]["request_tool_names"]
    assert names == ["file_read", "grep"]
    assert all(isinstance(name, str) for name in names)
    # The schema bodies must not ride along.
    assert "parameters" not in json.dumps(payloads[0])
