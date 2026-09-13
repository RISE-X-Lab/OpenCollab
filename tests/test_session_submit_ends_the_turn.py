"""``submit`` ends the turn on purpose, where every other step loops back.

A turn ends today when the model returns a message with no tool calls, so the
loop cannot tell "the work is done" from "the model stopped calling tools".
`submit` gives the first one a positive act without moving where a run stops:
a turn with no tool call still finishes exactly as it did, which is what keeps
runs made before and after this tool comparable.

The tests cover both halves of that sentence -- the submitted turn ends, and
the unsubmitted turn is untouched -- plus the two things that make a submitted
turn useful downstream: it is DONE (a success terminal, not a stop) and its
answer is the summary the model gave.
"""

from __future__ import annotations

from session_run_loop_test_support import (
    FakeLLM,
    FakeToolExecution,
    build_runner,
    llm_response,
    run,
    tool_call,
)

from opencollab.adapters.tools.submit import MAX_SUMMARY_CHARS, SubmitTool
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import ToolProcessingResult

SUMMARY = "Fixed the off-by-one in _rows() and ran the suite."


def _runner(*, submitted: bool, responses):
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    result = ToolProcessingResult(
        messages_to_append=[{"role": "tool", "tool_call_id": "t1", "content": "ok"}],
        turn_submitted=submitted,
        submitted_summary=SUMMARY if submitted else None,
    )
    llm = FakeLLM(responses=responses)
    return build_runner(state=state, llm=llm, tool_execution=FakeToolExecution(result)), llm


def test_a_submitted_step_ends_the_turn_at_done_with_its_summary():
    # Two responses are scripted; only the first should ever be consumed.
    runner, llm = _runner(
        submitted=True,
        responses=[
            llm_response(tool_calls=[tool_call("t1", "submit", '{"summary": "s"}')]),
            llm_response(content="this second turn must never run"),
        ],
    )

    answer = run(runner.run_loop())

    assert runner.state.phase is SessionPhase.DONE
    assert runner.state.terminal_reason == "submitted"
    assert answer == SUMMARY
    assert len(llm.calls) == 1


def test_a_step_that_did_not_submit_still_loops_back_to_another_call():
    runner, llm = _runner(
        submitted=False,
        responses=[
            llm_response(tool_calls=[tool_call("t1", "bash", '{"command": "ls"}')]),
            llm_response(content="done exploring"),
        ],
    )

    answer = run(runner.run_loop())

    # The rule that ends an unsubmitted turn is unchanged: a text-only response
    # finishes it, and it takes a second provider call to get there.
    assert runner.state.phase is SessionPhase.DONE
    assert answer == "done exploring"
    assert len(llm.calls) == 2


def test_submit_is_a_success_terminal_not_a_stop():
    runner, _ = _runner(
        submitted=True,
        responses=[llm_response(tool_calls=[tool_call("t1", "submit", '{"summary": "s"}')])],
    )

    run(runner.run_loop())

    # STOPPED is the controlled-halt terminal and the scheduler turns it into
    # "Error: agent stopped"; an agent that said it was finished must not be
    # reported as one that was cut off.
    assert runner.state.phase is not SessionPhase.STOPPED
    assert runner.state.phase is SessionPhase.DONE


def _call(tool, params):
    # The tool never touches the runtime; None keeps the test to the one thing
    # it is about.
    return run(tool.execute_with_runtime(params, None))


def test_the_tool_refuses_an_empty_or_oversized_summary():
    tool = SubmitTool()

    assert _call(tool, {}).startswith("Error")
    assert tool.turn_submitted is False
    assert _call(tool, {"summary": "   "}).startswith("Error")
    assert tool.turn_submitted is False
    assert _call(tool, {"summary": "x" * (MAX_SUMMARY_CHARS + 1)}).startswith("Error")
    assert tool.turn_submitted is False


def test_a_bad_call_after_a_good_one_does_not_leave_the_turn_submitted():
    tool = SubmitTool()

    _call(tool, {"summary": SUMMARY})
    assert tool.turn_submitted is True

    # One instance serves every step of a session, so the flag has to be reset
    # on entry rather than only set on success.
    _call(tool, {"summary": ""})
    assert tool.turn_submitted is False
    assert tool.submitted_summary is None


def test_the_real_executor_carries_the_flag_out_of_the_batch():
    """The wiring, end to end, with the tool the model actually calls.

    Everything above this point runs against a fake executor holding a
    pre-built result, which would keep passing if the executor stopped reading
    the flag off the tool. This one calls the real ``submit`` through the real
    dispatcher, and also pins the batch behaviour: a call the model issued
    after ``submit`` cannot run, because the turn is over.
    """
    from tool_execution_test_support import (
        AlwaysAllowPermissionPolicy,
        FakeAgent,
        NullEventPublisher,
    )

    from opencollab.application.tool_execution import ToolExecutionUseCase

    use_case = ToolExecutionUseCase(
        agent=FakeAgent([SubmitTool()]),
        environment=object(),
        state=SessionState(messages=[]),
        event_publisher=NullEventPublisher(),
        safety_policy=object(),
        permission_policy=AlwaysAllowPermissionPolicy(),
    )

    result = run(
        use_case.process(
            [
                {
                    "id": "c1",
                    "function": {
                        "name": "submit",
                        "arguments": '{"summary": "%s"}' % SUMMARY,
                    },
                },
                {
                    "id": "c2",
                    "function": {"name": "submit", "arguments": '{"summary": "again"}'},
                },
            ]
        )
    )

    assert result.turn_submitted is True
    assert result.submitted_summary == SUMMARY
    assert len(result.messages_to_append) == 2
    assert "Submitted" in result.messages_to_append[0]["content"]
    assert result.messages_to_append[1]["tool_call_id"] == "c2"
    assert "Submitted" not in result.messages_to_append[1]["content"]
