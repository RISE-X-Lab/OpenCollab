"""Required-tool corrections preserve the active tool restriction and usage."""

from __future__ import annotations

import pytest
from session_run_loop_test_support import (
    FakeLLM,
    FakeToolExecution,
    _agent_with_tool_schemas,
    build_runner,
    llm_response,
    run,
    tool_call,
)

from opencollab.application.steering import READS_NUDGE_HARD
from opencollab.domain.session import SessionPhase, SessionState, TurnEnforcementState
from opencollab.domain.tools import ToolProcessingResult


def state():
    return SessionState(
        messages=[{"role": "user", "content": "fix the source"}],
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )


@pytest.mark.parametrize("first_reply", ["I will edit it", None])
def test_required_write_retries_once_then_executes_the_write(first_reply):
    write = tool_call(name="file_write")
    llm = FakeLLM([llm_response(content=first_reply), llm_response(tool_calls=[write]), llm_response(content="done")])
    executor = FakeToolExecution(
        ToolProcessingResult(
            write_succeeded=True, messages_to_append=[{"role": "tool", "tool_call_id": "call-1", "content": "written"}]
        )
    )
    runner = build_runner(
        state=state(), llm=llm, tool_execution=executor, agent=_agent_with_tool_schemas("file_read", "file_write")
    )
    assert run(runner.run_loop()) == "done"
    assert [x["tool_choice"] for x in llm.calls[:2]] == ["required", "required"]
    assert [x["function"]["name"] for x in llm.calls[1]["tools"]] == ["file_write"]
    assert executor.calls == [[write]]
    assert runner.state.used_tokens == 15


def test_repeated_prose_cannot_complete_a_required_write():
    llm = FakeLLM([llm_response(content="done"), llm_response(content="done")])
    runner = build_runner(state=state(), llm=llm, agent=_agent_with_tool_schemas("file_write"))
    run(runner.run_loop())
    assert runner.state.phase is SessionPhase.STOPPED
    assert "required tool" in runner.state.terminal_reason
    assert len(llm.calls) == 2
    assert runner.state.used_tokens == 10


def test_new_user_turn_resets_required_tool_retry():
    runner = build_runner()
    runner._required_tool_retried = True
    runner.reset_runtime_for_user_turn()
    assert runner._required_tool_retried is False
