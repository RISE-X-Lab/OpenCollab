"""Session run-loop steering tests."""

from __future__ import annotations

import json

from session_run_loop_test_support import (
    FakeLLM,
    FakeToolExecution,
    FakeTracer,
    _agent_with_tool_schemas,
    _agent_with_tools,
    _no_consecutive_same_role,
    _steering_runner,
    build_runner,
    llm_response,
    run,
    tool_call,
)

from opencollab.application.steering import (
    READS_NUDGE_HARD,
    READS_NUDGE_SOFT,
    build_steering_block,
)
from opencollab.domain.session import (
    SessionState,
    TurnEnforcementState,
)
from opencollab.domain.tools import ToolProcessingResult


def test_write_steering_leaves_room_to_recover_from_a_failed_edit():
    assert READS_NUDGE_SOFT == 8
    assert READS_NUDGE_HARD == 16


def test_write_steering_does_not_force_an_edit_before_hard_threshold():
    message, override, level = build_steering_block(
        used_tokens=1_000,
        max_budget_tokens=100_000,
        step_count=4,
        max_steps=60,
        reads=14,
        has_write=True,
        has_structured_output=False,
        structured_override=None,
    )

    assert override is None
    assert level == "soft"
    assert "make it now" in message["content"]


def test_steering_status_line_built_from_budget_and_steps():
    # 120k of 500k used -> 380k left; 40 - 10 steps -> ~30 left.
    msg, override, _level = build_steering_block(
        used_tokens=120_000, max_budget_tokens=500_000, step_count=10, max_steps=40,
        reads=0, has_write=True, has_structured_output=False, structured_override=None,
    )
    assert override is None
    assert msg["role"] == "user"
    assert "380k/500k tokens left" in msg["content"]
    assert "~30 steps left" in msg["content"]

def test_steering_status_only_when_reads_are_low():
    # The status block is ALWAYS built; on a fresh post-user turn reads is ~0 so
    # only the status line comes back (no write nudge).
    msg, override, _level = build_steering_block(
        used_tokens=10_000, max_budget_tokens=100_000, step_count=1, max_steps=20,
        reads=0, has_write=True, has_structured_output=False, structured_override=None,
    )
    assert override is None
    assert msg["role"] == "user"
    assert "tokens left" in msg["content"]
    assert "without" not in msg["content"]  # status-line only, no read nudge

def test_steering_no_write_nudge_for_readonly_session():
    # A scout/tester/planner has no write tool — the write nudge would be nonsense,
    # so only the status line is shown even at a high read count.
    msg, override, _level = build_steering_block(
        used_tokens=0, max_budget_tokens=100_000, step_count=0, max_steps=20,
        reads=99, has_write=False, has_structured_output=False, structured_override=None,
    )
    assert override is None
    assert "without" not in msg["content"]
    assert msg["content"].startswith("[Budget:")

def test_steering_hard_rung_forces_tool_choice_through_run_loop():
    # End-to-end: a write-capable session at the hard read threshold must send
    # tool_choice="required" to the provider through the real _complete path.
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state, llm=llm, agent=_agent_with_tools("file_read", "apply_patch")
    )

    run(runner.run_loop())

    assert llm.calls[0]["tool_choice"] == "required"

def test_steering_structured_hard_rung_forces_structured_output():
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("structured_output", "file_read", "grep"),
    )

    run(runner.run_loop())

    sent_tool_names = [spec["function"]["name"] for spec in llm.calls[0]["tools"]]
    assert sent_tool_names == ["structured_output"]
    assert llm.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "structured_output"},
    }
    assert "structured_output using" in llm.calls[0]["messages"][-1]["content"]

def test_steering_hard_rung_blocks_read_tool_call_before_execution():
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    read_call = tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}')
    llm = FakeLLM(
        [
            llm_response(content="try read", tool_calls=[read_call], finish_reason="tool_calls"),
            llm_response(content="done"),
        ]
    )
    tool_execution = FakeToolExecution()
    runner = build_runner(
        state=state,
        llm=llm,
        tool_execution=tool_execution,
        agent=_agent_with_tool_schemas("file_read", "file_write", "apply_patch"),
    )

    result = run(runner.run_loop())

    tool_messages = [
        m for m in state.messages if m.get("role") == "tool" and m.get("tool_call_id")
    ]
    assert result == "done"
    assert tool_execution.calls == []
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "r1"
    assert "not allowed during the hard write gate" in tool_messages[0]["content"]
    assert state.turn.reads_since_last_edit == READS_NUDGE_HARD

def test_steering_hard_rung_executes_allowed_write_from_mixed_batch():
    state = SessionState(
        messages=[{"role": "tool", "content": "prev"}],
        used_tokens=1_000,
        step_count=1,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )
    read_call = tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}')
    write_call = tool_call(call_id="w1", name="apply_patch", arguments='{"patch": "..."}')
    llm = FakeLLM(
        [
            llm_response(
                content="mixed",
                tool_calls=[read_call, write_call],
                finish_reason="tool_calls",
            ),
            llm_response(content="done"),
        ]
    )
    tool_execution = FakeToolExecution(
        ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "w1", "content": "patched"}],
            write_succeeded=True,
        )
    )
    runner = build_runner(
        state=state,
        llm=llm,
        tool_execution=tool_execution,
        agent=_agent_with_tool_schemas("file_read", "file_write", "apply_patch"),
    )

    result = run(runner.run_loop())

    tool_messages = [
        m for m in state.messages if m.get("role") == "tool" and m.get("tool_call_id")
    ]
    assert result == "done"
    assert tool_execution.calls == [[write_call]]
    assert [m["tool_call_id"] for m in tool_messages] == ["r1", "w1"]
    assert "not allowed during the hard write gate" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == "patched"
    assert state.turn.reads_since_last_edit == 0


def _failed_edit_state(
    arguments: str, result: str, *, tool_name: str = "apply_patch"
) -> SessionState:
    return SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "tool_calls": [tool_call(
                    call_id="failed-edit",
                    name=tool_name,
                    arguments=arguments,
                )],
            },
            {"role": "tool", "tool_call_id": "failed-edit", "content": result},
        ],
        used_tokens=1_000,
        step_count=3,
        turn=TurnEnforcementState(reads_since_last_edit=READS_NUDGE_HARD),
    )


def test_failed_edit_gets_one_targeted_read_turn_before_write_gate():
    target = "pkg/module.py"
    state = _failed_edit_state(
        json.dumps({"path": target, "mode": "unified_diff", "patch": "bad hunk"}),
        f"Error applying patch to {target}: hunk did not match",
    )
    read_call = tool_call(
        call_id="recovery-read",
        name="file_read",
        arguments=json.dumps({"path": target, "offset": 1, "limit": 20}),
    )
    llm = FakeLLM([
        llm_response(tool_calls=[read_call], finish_reason="tool_calls"),
        llm_response(content="done"),
    ])
    execution = FakeToolExecution(ToolProcessingResult(
        messages_to_append=[{
            "role": "tool",
            "tool_call_id": "recovery-read",
            "content": "current source",
        }],
        reads_executed=1,
    ))
    runner = build_runner(
        state=state,
        llm=llm,
        tool_execution=execution,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
    )

    assert run(runner.run_loop()) == "done"
    assert execution.calls == [[read_call]]
    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == [
        "file_read",
        "grep",
    ]
    assert target in llm.calls[0]["messages"][-1]["content"]
    assert [spec["function"]["name"] for spec in llm.calls[1]["tools"]] == [
        "file_write",
        "apply_patch",
    ]


def test_noop_edit_gets_one_targeted_read_turn_before_write_gate():
    target = "pkg/module.py"
    state = _failed_edit_state(
        json.dumps({
            "path": target,
            "mode": "str_replace",
            "old_str": "same",
            "new_str": "same",
        }),
        f"Error: str_replace was a no-op; nothing changed in {target}.",
        tool_name="file_write",
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
    )

    run(runner.call_llm(runner.build_tool_schemas()))

    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == [
        "file_read",
        "grep",
    ]
    assert target in llm.calls[0]["messages"][-1]["content"]


def test_noop_patch_gets_one_targeted_read_turn_before_write_gate():
    target = "pkg/module.py"
    state = _failed_edit_state(
        json.dumps({
            "path": target,
            "mode": "line_replace",
            "start_line": 1,
            "end_line": 1,
            "new_str": "same",
        }),
        f"Error: patch was a no-op; nothing changed in {target}.",
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
    )

    run(runner.call_llm(runner.build_tool_schemas()))

    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == [
        "file_read",
        "grep",
    ]


def test_failed_edit_recovery_blocks_reading_another_path():
    target = "pkg/module.py"
    state = _failed_edit_state(
        json.dumps({"path": target, "mode": "unified_diff", "patch": "bad hunk"}),
        f"Error applying patch to {target}: hunk did not match",
    )
    wrong_read = tool_call(
        call_id="wrong-read",
        name="file_read",
        arguments=json.dumps({"path": "other.py"}),
    )
    llm = FakeLLM([
        llm_response(tool_calls=[wrong_read], finish_reason="tool_calls"),
        llm_response(content="done"),
    ])
    execution = FakeToolExecution()
    runner = build_runner(
        state=state,
        llm=llm,
        tool_execution=execution,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
    )

    assert run(runner.run_loop()) == "done"
    assert execution.calls == []
    blocked = next(
        message for message in state.messages
        if message.get("tool_call_id") == "wrong-read"
    )
    assert "failed-edit recovery gate" in blocked["content"]


def test_invalid_edit_arguments_do_not_open_recovery_read_gate():
    state = _failed_edit_state(
        "not-json",
        "Error: invalid JSON arguments: not-json",
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
    )

    run(runner.call_llm(runner.build_tool_schemas()))

    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == [
        "file_write",
        "apply_patch",
    ]

def test_steering_reaches_provider_and_is_persisted():
    # The steering block is SENT to the model AND persisted to state.messages, so
    # the budget the model saw is saved to the transcript. History ends on a user
    # turn, so the block is folded into it IN PLACE (no extra message).
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        used_tokens=1_000,
        step_count=1,
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, llm=llm, max_budget_tokens=100_000, max_steps=10)

    run(runner.run_loop())

    sent = llm.calls[0]["messages"]
    assert any(m["role"] == "user" and "Budget:" in (m.get("content") or "") for m in sent)
    # Persisted by folding into the user turn — no new message added.
    assert any("Budget:" in (m.get("content") or "") for m in state.messages)
    assert "go" in state.messages[1]["content"] and "Budget:" in state.messages[1]["content"]

def test_steering_folds_into_last_user_message_no_double_user():
    # When the shaped history ENDS WITH A USER message (the default chat/team path:
    # the lead answers right after the user speaks), the status must still reach the
    # model — folded into that user message, not appended as a second user turn.
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fix the bug"},
        ],
        used_tokens=50_000,
        step_count=2,
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, llm=llm, max_budget_tokens=200_000, max_steps=20)

    run(runner.call_llm(runner.build_tool_schemas()))

    sent = llm.calls[0]["messages"]
    # (a) the outgoing prompt's LAST message is the user turn carrying the status.
    assert sent[-1]["role"] == "user"
    assert "fix the bug" in sent[-1]["content"]  # original content preserved
    assert "tokens left" in sent[-1]["content"]  # status folded in
    # (b) no two consecutive user messages (Anthropic's alternation contract).
    assert _no_consecutive_same_role(sent)
    # (c) state.messages IS updated — the budget is persisted into the user turn,
    #     preserving its original text.
    assert "fix the bug" in state.messages[-1]["content"]
    assert "tokens left" in state.messages[-1]["content"]

def test_steering_folds_into_list_content_user_message():
    # Provider content-parts form: the fold appends a text part as a NEW list and
    # persists it into state.messages (the original ``parts`` list is left intact).
    parts = [{"type": "text", "text": "fix the bug"}]
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": parts},
        ],
        used_tokens=50_000,
        step_count=2,
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, llm=llm, max_budget_tokens=200_000, max_steps=20)

    run(runner.call_llm(runner.build_tool_schemas()))

    sent_last = llm.calls[0]["messages"][-1]
    assert sent_last["role"] == "user"
    assert isinstance(sent_last["content"], list)
    assert any("tokens left" in p.get("text", "") for p in sent_last["content"])
    # state.messages list-content now carries the persisted budget part (the new
    # list does not mutate the caller's original ``parts`` object).
    assert len(state.messages[-1]["content"]) == 2
    assert state.messages[-1]["content"][0] == {"type": "text", "text": "fix the bug"}
    assert any("tokens left" in p.get("text", "") for p in state.messages[-1]["content"])
    assert parts == [{"type": "text", "text": "fix the bug"}]  # original untouched

def test_steering_block_ephemeral_on_continuation_step():
    # On a continuation step (history ends with a tool message, not a user turn)
    # the steering block (status + hard write-nudge) reaches the provider in the
    # shaped copy, but stays OUT of the persisted state.messages — there is no
    # user turn to fold it into, and a standalone block would pollute the
    # transcript / break role alternation.
    tracer = FakeTracer()
    runner, state = _steering_runner(READS_NUDGE_HARD, tracer=tracer)
    run(runner.call_llm(runner.build_tool_schemas()))

    sent = runner.llm.calls[0]["messages"]
    assert any("STOP reading" in (m.get("content") or "") for m in sent)
    assert any("Budget:" in (m.get("content") or "") for m in sent)
    # Not persisted: the model saw it, the transcript did not gain it.
    assert not any("STOP reading" in (m.get("content") or "") for m in state.messages)
    assert not any("Budget:" in (m.get("content") or "") for m in state.messages)
