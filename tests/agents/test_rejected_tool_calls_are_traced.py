"""A tool call the runtime refuses before execution leaves a trace record."""

from __future__ import annotations

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session as Session
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    FakeTool,
    FakeTracer,
    event_collector,
    llm_response,
    run,
    tool_call,
)


def _run_session(calls):
    known_tool = FakeTool(name="known_tool")
    fake_llm = FakeLLMClient(
        [
            llm_response(tool_calls=calls, finish_reason="tool_calls"),
            llm_response(content="recovered"),
        ]
    )
    tracer = FakeTracer()
    _, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[known_tool]),
        llm=fake_llm,
        tracer=tracer,
        event_sink=EventBus(on_event),
    )
    assert run(session.run_loop()) == "recovered"
    assert known_tool.calls == []
    return session, [step for step in tracer.steps if step["step_type"] == "tool_error"]


def test_out_of_role_call_in_a_session_is_traced():
    _, errors = _run_session([tool_call(name="missing_tool", arguments='{"value": 1}')])

    assert len(errors) == 1
    payload = errors[0]["payload"]
    assert payload["tool"] == "missing_tool"
    assert payload["error"] == "unknown_tool"
    assert payload["batch_rejected"] is False


def test_every_call_of_a_rejected_batch_is_traced():
    calls = [
        tool_call(name="known_tool", arguments='{"value": 1}', call_id="call-ok"),
        tool_call(name="missing_tool", arguments='{"value": 2}', call_id="call-out"),
    ]

    session, errors = _run_session(calls)

    # The model-facing replies are unchanged: both calls answer with the batch refusal.
    replies = [m for m in session.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in replies] == ["call-ok", "call-out"]
    assert all("entire tool-call batch rejected" in m["content"] for m in replies)

    assert [step["payload"]["tool"] for step in errors] == ["known_tool", "missing_tool"]
    assert [step["payload"]["batch_index"] for step in errors] == [0, 1]
    assert all(step["payload"]["batch_rejected"] is True for step in errors)
    assert [step["payload"]["tool_call_id"] for step in errors] == ["call-ok", "call-out"]
    assert errors[0]["payload"]["error"] == "rejected_with_batch"
    assert errors[1]["payload"]["error"] == "unknown_tool"
