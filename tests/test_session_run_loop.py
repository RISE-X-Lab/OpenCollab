from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.application.session_run import (
    GenerationTimeoutError,
    PendingStep,
    SessionRunUseCase,
)
from opencollab.application.steering import READS_NUDGE_HARD, build_steering_block
from opencollab.domain.pending import RowKind, RowStatus
from opencollab.domain.session import (
    SessionPhase,
    SessionState,
    TurnEnforcementState,
)
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


def llm_response(
    content=None,
    tool_calls=None,
    total_tokens=5,
    input_tokens=1,
    finish_reason="stop",
    reasoning=None,
    output_tokens=0,
):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated=False,
        ),
        finish_reason=finish_reason,
        reasoning=reasoning,
    )


def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeAgent:
    model = "fake-model"
    temperature = 0.35

    def __init__(self, schemas=None):
        self._schemas = schemas if schemas is not None else []

    def tool_schemas(self):
        return copy.deepcopy(self._schemas)


class FakeLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "temperature": temperature,
                **kwargs,  # tool_choice / thinking ride here when set
            }
        )
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)


class FakeToolExecution:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ToolProcessingResult()

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        return self.result


class FakeToolExecutionDeferred:
    """Tool executor that also handles deferrable tools via ``execute_deferred``.

    ``deferred_outcomes`` maps a tool_call_id -> (ref, error): a non-None ref
    means the tool deferred work (returns a child aid to await); a non-None
    error means it resolved synchronously and fills its row at once.
    """

    def __init__(self, process_result=None, deferred_outcomes=None):
        self.process_calls = []
        self.deferred_calls = []
        self.process_result = process_result if process_result is not None else ToolProcessingResult()
        self.deferred_outcomes = deferred_outcomes or {}

    async def process(self, tool_calls):
        self.process_calls.append(copy.deepcopy(tool_calls))
        return self.process_result

    async def execute_deferred(self, tc):
        self.deferred_calls.append(copy.deepcopy(tc))
        return self.deferred_outcomes.get(tc["id"], (None, "no outcome"))


class CompletingBeforeReturnToolExecution(FakeToolExecutionDeferred):
    """Simulate a child that finishes before ``execute_deferred`` returns."""

    def __init__(self, state: SessionState):
        super().__init__(deferred_outcomes={"s1": (7, None)})
        self.state = state

    async def execute_deferred(self, tc):
        self.deferred_calls.append(copy.deepcopy(tc))
        row = self.state.pending_events.rows[tc["id"]]
        assert row.status is RowStatus.PENDING
        self.state.pending_events.fill(tc["id"], result="fast child result")
        return self.deferred_outcomes[tc["id"]]


class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens=0, latency=0.0):
        self.steps.append(
            {
                "step_type": step_type,
                "payload": copy.deepcopy(payload),
                "tokens": tokens,
                "latency": latency,
            }
        )

    def flush(self):
        self.flush_count += 1


def collect_events():
    events = []

    async def sink(event):
        events.append((event.type, copy.deepcopy(event.data)))

    return events, EventBus(sink)


def build_runner(
    *,
    state=None,
    agent=None,
    llm=None,
    event_bus=None,
    tool_execution=None,
    tracer=None,
    **kwargs,
):
    state = state if state is not None else SessionState(messages=[{"role": "system", "content": "sys"}])
    return SessionRunUseCase(
        agent=agent if agent is not None else FakeAgent(),
        state=state,
        llm=llm if llm is not None else FakeLLM([llm_response(content="done")]),
        event_publisher=event_bus if event_bus is not None else EventBus(None),
        tool_execution=tool_execution if tool_execution is not None else FakeToolExecution(),
        tracer=tracer,
        **kwargs,
    )


@pytest.mark.parametrize(
    "per_call_timeout",
    [0, -1, float("nan"), float("inf"), True, "bad"],
)
def test_runner_rejects_unbounded_per_call_timeout(per_call_timeout):
    with pytest.raises(ValueError, match="finite positive"):
        build_runner(per_call_timeout=per_call_timeout)


def test_run_loop_budget_exceeded_emits_error_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=10)
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus, max_budget_tokens=10)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "budget exceeded: 10 tokens used"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Budget exceeded: 10 tokens used. Session stopped.]",
    }
    assert events == [("error", {"reason": "budget exceeded: 10 tokens used", "aid": -1})]


def test_run_loop_team_aggregate_ceiling_stops_under_own_cap():
    # Per-session cap is generous (1_000_000) and the session has spent nothing,
    # so the per-session check passes — but the injected team-aggregate predicate
    # reports the global pool exhausted, so the session must still stop.
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=0)
    llm = FakeLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        max_budget_tokens=1_000_000,
        team_budget_exhausted=lambda: True,
    )

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []  # never called the model
    assert state.phase is SessionPhase.STOPPED
    assert "team budget exceeded" in state.terminal_reason
    assert events == [
        ("error", {"reason": "team budget exceeded: aggregate spend reached the global cap", "aid": -1})
    ]


def test_run_loop_team_aggregate_ceiling_not_reached_proceeds_normally():
    # When the aggregate predicate reports headroom, the per-session loop runs as
    # before (regression guard that the new check is additive, not a hard stop).
    events, bus = collect_events()
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        llm=llm,
        event_bus=bus,
        max_budget_tokens=1_000_000,
        team_budget_exhausted=lambda: False,
    )

    result = run(runner.run_loop())

    assert result == "done"
    assert len(llm.calls) == 1


def test_run_loop_step_limit_exceeded_emits_error_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], step_count=3)
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus, max_steps=3)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "step limit reached: 3 steps"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Step limit reached: 3 steps. Session stopped.]",
    }
    assert events == [("error", {"reason": "step limit reached: 3 steps", "aid": -1})]


def test_new_turn_precheck_failure_does_not_return_previous_answer():
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "second question"},
        ],
        step_count=3,
    )
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, max_steps=3)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED


def test_run_loop_cancel_event_appends_interrupt_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus)
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = run(runner.run_loop(cancel_event=cancel_event))

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.messages[-1] == {"role": "system", "content": "[Session interrupted by user]"}
    assert events == [("error", {"reason": "interrupted by user", "aid": -1})]


def test_run_loop_loop_block_limit_stops_before_next_llm_call():
    events, bus = collect_events()
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        turn=TurnEnforcementState(loop_blocked_since_progress=3),
    )
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "loop block limit reached: 3 repeated tool calls"
    assert events == [("error", {"reason": "loop block limit reached: 3 repeated tool calls", "aid": -1})]


def test_run_loop_llm_step_events_trace_and_message_shape():
    events, bus = collect_events()
    schemas = [{"type": "function", "function": {"name": "fake_tool"}}]
    tool_calls = [tool_call()]
    llm = FakeLLM(
        [
            llm_response(content="need tool", tool_calls=tool_calls, total_tokens=9, finish_reason="tool_calls"),
            llm_response(content="done", total_tokens=4),
        ]
    )
    tracer = FakeTracer()
    tool_execution = FakeToolExecution(
        ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]
        )
    )
    runner = build_runner(
        agent=FakeAgent(schemas),
        llm=llm,
        event_bus=bus,
        tool_execution=tool_execution,
        tracer=tracer,
    )

    result = run(runner.run_loop())

    assert result == "done"
    assert llm.calls[0]["tools"] == schemas
    assert llm.calls[0]["temperature"] == 0.35
    assert tool_execution.calls == [tool_calls]
    assert [event_type for event_type, _data in events] == [
        "step_start",
        "text_delta",
        "step_end",
        "step_start",
        "text_delta",
        "step_end",
    ]
    assert events[0] == ("step_start", {"step": 1, "aid": -1})
    assert events[2][1]["step"] == 1
    assert events[2][1]["latency"] == tracer.steps[0]["latency"]
    assert tracer.steps[0] == {
        "step_type": "llm_call",
        "payload": {
            "model": "fake-model",
            "thinking": False,
            "finish_reason": "tool_calls",
            "content": "need tool",
            "tool_calls": [{"id": "call-1", "name": "fake_tool", "arguments": '{"value": 1}'}],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 9,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "uncached_input_tokens": 1,
                "estimated": False,
            },
        },
        "tokens": 9,
        "latency": tracer.steps[0]["latency"],
    }


# ---------------------------------------------------------------------------
# Closed-loop steering block (budget self-awareness + reads-without-write)
# ---------------------------------------------------------------------------


class _ToolStub:
    def __init__(self, name):
        self.name = name


def _tool_schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _agent_with_tools(*names):
    agent = FakeAgent()
    agent.tools = [_ToolStub(n) for n in names]
    return agent


def _agent_with_tool_schemas(*names):
    agent = FakeAgent([_tool_schema(name) for name in names])
    agent.tools = [_ToolStub(name) for name in names]
    return agent


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


# --------------------------------------------------------------------------- #
# P0 observability — trace steering nudges on upward crossings
# --------------------------------------------------------------------------- #


def _steering_steps(tracer):
    return [s for s in tracer.steps if s["step_type"] == "steering_nudge"]


def _steering_runner(reads, *, tools=("file_read", "apply_patch"), tracer=None, aid=7):
    # History ends with a tool message (not user) so the steering block is built.
    state = SessionState(
        messages=[{"role": "tool", "content": "r"}],
        used_tokens=1_000,
        step_count=3,
        turn=TurnEnforcementState(reads_since_last_edit=reads),
        aid=aid,
    )
    llm = FakeLLM([llm_response(content="done") for _ in range(8)])
    return build_runner(
        state=state,
        llm=llm,
        tracer=tracer,
        agent=_agent_with_tools(*tools),
        max_budget_tokens=100_000,
        max_steps=40,
    ), state


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


def test_run_loop_without_tool_calls_marks_done():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(
        state=state,
        llm=FakeLLM([llm_response(content="plain", total_tokens=6)]),
        event_bus=bus,
    )

    result = run(runner.run_loop())

    assert result == "plain"
    assert state.is_done is True
    assert state.phase is SessionPhase.DONE
    assert state.used_tokens == 6
    assert state.messages[-1] == {"role": "assistant", "content": "plain"}
    assert [event_type for event_type, _data in events] == ["step_start", "text_delta", "step_end"]


# ---------------------------------------------------------------------------
# Empty-stop retry — a finish_reason="stop" turn with no text and no tool calls
# ---------------------------------------------------------------------------


def _no_consecutive_same_role(messages):
    """No two adjacent messages share a role (the role-alternation contract)."""
    return all(a["role"] != b["role"] for a, b in zip(messages, messages[1:]))


def _convo():
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]


def test_empty_stop_retries_once_with_nudge_then_succeeds():
    from opencollab.application.session_run import _EMPTY_STOP_NUDGE, _EMPTY_STOP_PLACEHOLDER

    state = SessionState(messages=_convo())
    llm = FakeLLM(
        [
            llm_response(content=None),  # empty-stop
            llm_response(content="recovered", total_tokens=4),
        ]
    )
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == "recovered"
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 2  # initial empty turn + one retry
    # The retry call saw the nudge as a user message. Because that retry turn's
    # shaped history ends with the user nudge, the per-turn status block folds INTO
    # it (rather than appending a second consecutive user turn), so the nudge text
    # rides in a user message alongside the status rather than as a standalone dict.
    retry_sent = llm.calls[1]["messages"]
    assert any(
        m["role"] == "user" and _EMPTY_STOP_NUDGE in (m.get("content") or "")
        for m in retry_sent
    )
    assert _no_consecutive_same_role(retry_sent)
    # The nudge is persisted to state.messages, preceded by an assistant
    # placeholder so roles still alternate (no two consecutive user messages,
    # which Anthropic rejects). The retry's per-turn budget block folds into that
    # same user turn, so the nudge now leads a user message rather than standing
    # alone as a bare dict.
    assert any(
        m["role"] == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(_EMPTY_STOP_NUDGE)
        for m in state.messages
    )
    assert _no_consecutive_same_role(state.messages)
    assert {"role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER} in state.messages
    # No bare empty assistant message was ever appended.
    assert all(
        not (m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls"))
        for m in state.messages
    )
    assert state.messages[-1] == {"role": "assistant", "content": "recovered"}


def test_empty_stop_retries_at_most_once_then_gives_up():
    # Two consecutive empty-stops: retry fires once, then the session finishes
    # cleanly rather than looping. A 3rd LLM call would make FakeLLM raise.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None), llm_response(content=None)])
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == ""  # placeholder is filtered out, never returned as an answer
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 2  # bounded: initial + exactly one retry


def test_empty_stop_retry_is_recorded_in_trajectory():
    # The retry must leave a grep-able trace entry, since the injected
    # nudge/placeholder messages are never persisted to the trajectory.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None), llm_response(content="ok", total_tokens=4)])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    retries = [s for s in tracer.steps if s["step_type"] == "empty_stop_retry"]
    assert len(retries) == 1
    assert retries[0]["payload"] == {"finish_reason": "stop", "had_reasoning": False}


def test_llm_trace_records_reasoning_when_present():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer", reasoning="step-by-step thoughts")])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["reasoning"] == "step-by-step thoughts"


def test_llm_trace_records_effective_thinking_mode():
    agent = FakeAgent()
    agent.thinking = True
    tracer = FakeTracer()
    runner = build_runner(agent=agent, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["thinking"] is True


def test_reasoning_is_preserved_in_assistant_tool_call_history():
    state = SessionState(messages=_convo())
    response = llm_response(
        tool_calls=[tool_call()],
        reasoning="I need to inspect the file first.",
    )
    runner = build_runner(state=state)

    runner.append_assistant_message(response)

    assert state.messages[-1]["reasoning_content"] == "I need to inspect the file first."
    assert state.messages[-1]["tool_calls"] == response.tool_calls


def test_llm_trace_omits_reasoning_when_absent():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer")])  # reasoning defaults to None
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert "reasoning" not in llm_calls[0]["payload"]


def test_empty_stop_with_length_finish_reason_does_not_retry():
    # A truncated turn (finish_reason="length") with empty content is NOT an
    # empty-stop: a nudge cannot un-truncate it, so we must not waste a retry.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None, finish_reason="length")])
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == ""
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 1  # no retry attempted


def test_empty_stop_retry_budget_resets_across_turns():
    # The once-per-turn flag must reset on a new turn, or a long-lived session
    # that hit an empty-stop in turn 1 would never retry again.
    state = SessionState(messages=_convo())
    llm = FakeLLM(
        [
            llm_response(content=None),  # turn 1 empty-stop
            llm_response(content="answer-1", total_tokens=4),
            llm_response(content=None),  # turn 2 empty-stop
            llm_response(content="answer-2", total_tokens=4),
        ]
    )
    runner = build_runner(state=state, llm=llm)

    assert run(runner.run_loop()) == "answer-1"
    assert len(llm.calls) == 2

    # Start a fresh user turn on the SAME runner instance.
    state.reset_for_user_turn()
    state.append_message({"role": "user", "content": "follow-up"})
    assert run(runner.run_loop()) == "answer-2"
    assert len(llm.calls) == 4  # turn 2 retried too — flag was reset


def test_deferred_spawn_suspends_then_resumes_after_fill():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="spawning",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="done after child", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (7, None)})
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    # First run: a deferred spawn suspends the session on AWAITING_EVENTS.
    result = run(runner.run_loop())
    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert result == "spawning"
    assert len(llm.calls) == 1
    row = state.pending_events.rows["s1"]
    assert row.tool_call_id == "s1"
    assert row.ref == 7
    assert row.kind is RowKind.CHILD_AGENT
    assert row.status is RowStatus.PENDING

    # The scheduler fills the row when the child finishes, then re-activates.
    state.pending_events.fill("s1", result="child result")
    result2 = run(runner.run_loop())

    assert result2 == "done after child"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    # The resumed LLM call saw the child result as a proper tool result.
    second_call_msgs = llm.calls[1]["messages"]
    assert {"role": "tool", "tool_call_id": "s1", "content": "child result"} in second_call_msgs


def test_deferred_row_exists_before_fast_child_can_complete():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="spawning",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="done after fast child", total_tokens=4),
        ]
    )
    te = CompletingBeforeReturnToolExecution(state)
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    result = run(asyncio.wait_for(runner.run_loop(), timeout=0.5))

    assert result == "done after fast child"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    assert {
        "role": "tool",
        "tool_call_id": "s1",
        "content": "fast child result",
    } in llm.calls[1]["messages"]


def test_mixed_batch_buffers_immediate_and_resumes_in_order():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    batch = [
        tool_call(call_id="b1", name="fake_tool", arguments="{}"),
        tool_call(call_id="s1", name="spawn_agent", arguments="{}"),
    ]
    llm = FakeLLM(
        [
            llm_response(content="mixed", tool_calls=batch, finish_reason="tool_calls"),
            llm_response(content="final", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(
        process_result=ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "b1", "content": "bash ok"}]
        ),
        deferred_outcomes={"s1": (9, None)},
    )
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    run(runner.run_loop())
    assert state.phase is SessionPhase.AWAITING_EVENTS
    # process() ran only the immediate tool; the deferred one bypassed it.
    assert te.process_calls == [[batch[0]]]
    assert len(state.pending_events.rows) == 2
    assert not state.pending_events.is_complete()  # deferred row still pending

    state.pending_events.fill("s1", result="child done")
    run(runner.run_loop())

    # Tool-result block is contiguous and in original tool_calls order.
    second_call_msgs = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert tool_msgs == [
        {"role": "tool", "tool_call_id": "b1", "content": "bash ok"},
        {"role": "tool", "tool_call_id": "s1", "content": "child done"},
    ]


def test_deferred_batch_with_blocked_tool_uses_original_order():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    state.phase = SessionPhase.EXECUTING_TOOLS
    blocked_read = tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}')
    deferred_spawn = tool_call(call_id="s1", name="spawn_agent", arguments="{}")
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (9, None)})
    runner = build_runner(state=state, tool_execution=te)
    runner._pending = PendingStep(
        response=llm_response(
            content="mixed",
            tool_calls=[blocked_read, deferred_spawn],
            finish_reason="tool_calls",
        ),
        latency=0.0,
    )
    runner._pending_tool_allowlist = frozenset({"spawn_agent"})
    runner._pending_tool_gate_label = "test gate"

    run(runner.execute_pending_tools())

    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert state.pending_events.rows["r1"].order == 0
    assert state.pending_events.rows["s1"].order == 1
    assert "not allowed during the test gate" in state.pending_events.rows["r1"].result
    assert te.process_calls == []
    assert te.deferred_calls == [deferred_spawn]


def test_mixed_batch_folds_in_reads_counter_for_immediate_reads():
    # Regression: a mixed batch (immediate read + deferred spawn) buffers its
    # result messages, so the reads-without-write counter must still fold in via
    # apply_read_write_counter_to — not be lost because only apply_hashes_to ran.
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    batch = [
        tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}'),
        tool_call(call_id="s1", name="spawn_agent", arguments="{}"),
    ]
    llm = FakeLLM(
        [llm_response(content="mixed", tool_calls=batch, finish_reason="tool_calls")]
    )
    te = FakeToolExecutionDeferred(
        process_result=ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "r1", "content": "file body"}],
            reads_executed=1,
        ),
        deferred_outcomes={"s1": (9, None)},
    )
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    run(runner.run_loop())

    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert state.turn.reads_since_last_edit == 1  # immediate read counted despite buffering


def test_deferred_rejected_synchronously_does_not_suspend():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="try spawn",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="handled error", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (None, "Permission denied: nope")})
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    result = run(runner.run_loop())

    # No event will ever arrive, so the session must not suspend — it drains
    # the synchronous error and finishes in one run.
    assert result == "handled error"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "s1", "content": "Permission denied: nope"}]


# ---------------------------------------------------------------------------
# Context-overflow safety net
# ---------------------------------------------------------------------------


class FakeOverflowError(Exception):
    """A context-overflow provider rejection stand-in (no real SDK / network)."""


def _is_overflow(exc):
    return isinstance(exc, FakeOverflowError)


class OverflowThenOkLLM:
    """Raises a context overflow on the FIRST ``complete`` call, then succeeds.

    Records the messages it was handed each call so a test can assert the
    retried (forced-compaction) prompt is smaller than the first attempt.
    """

    def __init__(self, ok_response):
        self.ok_response = ok_response
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        if len(self.calls) == 1:
            raise FakeOverflowError("prompt is too long")
        return self.ok_response


class AlwaysOverflowLLM:
    """Raises a context overflow on EVERY ``complete`` call."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        raise FakeOverflowError("prompt is too long")


class FakeForcedShaper:
    """A shaper that no-ops on the normal pass but compacts hard when forced.

    Mirrors the real reactive layers' ``_forced`` contract: ``shape`` returns
    the messages unchanged until ``forced_shape`` flips ``_forced`` on, at which
    point it drops all but the first (pinned) message — standing in for a
    maximal compaction pass.
    """

    def __init__(self):
        self._forced = False

    def shape(self, messages):
        if not self._forced:
            return list(messages)
        return messages[:1]


def test_call_llm_recompacts_and_retries_once_on_overflow():
    # A long history; the shaper no-ops normally (so the first call overflows),
    # then a forced compaction shrinks it and the retry succeeds.
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a" * 4000},
            {"role": "assistant", "content": "b" * 4000},
        ]
    )
    llm = OverflowThenOkLLM(llm_response(content="recovered"))
    runner = build_runner(
        state=state,
        llm=llm,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    result = run(runner.run_loop())

    assert result == "recovered"
    assert state.phase is SessionPhase.DONE
    # Two provider calls: the overflowing first, the forced-compacted retry.
    assert len(llm.calls) == 2
    # The retry's prompt is strictly smaller (forced compaction ran).
    assert len(llm.calls[1]) < len(llm.calls[0])
    assert llm.calls[1] == state.messages[:1]
    # The original history is untouched by shaping (read-time only); only the
    # new assistant answer was appended on success.
    assert state.messages[:3] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a" * 4000},
        {"role": "assistant", "content": "b" * 4000},
    ]
    assert state.messages[-1] == {"role": "assistant", "content": "recovered"}


def test_call_llm_emits_recompaction_event_on_overflow():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = OverflowThenOkLLM(llm_response(content="ok"))
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    run(runner.run_loop())

    reasons = [data["reason"] for etype, data in events if etype == "error"]
    assert "context_overflow_recompacted" in reasons


def test_persistent_overflow_stops_gracefully_not_unhandled():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = AlwaysOverflowLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    # No unhandled exception — the loop returns normally with a controlled stop.
    result = run(runner.run_loop())

    assert result == ""
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("context overflow")
    assert state.messages[-1] == {
        "role": "system",
        "content": (
            "[Context overflow: prompt exceeds the model context window "
            "even after compaction. Session stopped.]"
        ),
    }
    # Both the recompaction notice and the final overflow error were emitted.
    reasons = [data["reason"] for etype, data in events if etype == "error"]
    assert "context_overflow_recompacted" in reasons
    assert "context overflow: prompt exceeds the model context window even after compaction" in reasons
    # The provider was tried exactly twice (initial + one forced retry).
    assert len(llm.calls) == 2


def test_overflow_classifier_off_propagates_as_error():
    # Without an overflow classifier wired (standalone/legacy), an overflow is
    # an opaque exception: the run loop fails to ERROR and re-raises, exactly as
    # before the safety net existed (regression guard that the net is additive).
    import pytest

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = AlwaysOverflowLLM()
    runner = build_runner(state=state, llm=llm, shaper=FakeForcedShaper())

    with pytest.raises(FakeOverflowError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    # Only one call: with no classifier it isn't recognised, so no retry.
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Per-call generation timeout (P7): one slow generation can't eat the whole wall
# ---------------------------------------------------------------------------


class SlowLLM:
    """A provider whose ``complete`` awaits ``delay`` seconds before answering.

    Stands in for a death-slow thinking generation; with a small
    ``per_call_timeout`` the run loop must surface ``GenerationTimeoutError``.
    """

    def __init__(self, response, delay):
        self.response = response
        self.delay = delay
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        await asyncio.sleep(self.delay)
        return self.response


class CancelCleanupLLM:
    def __init__(self):
        self.calls = []
        self.cancel_seen = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        gate = asyncio.Event()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_cancel.wait()
            raise


def test_per_call_timeout_raises_on_slow_generation():
    import pytest

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = SlowLLM(llm_response(content="too late"), delay=1.0)
    runner = build_runner(state=state, llm=llm, per_call_timeout=0.01)

    # The single generation exceeds the 0.01s ceiling -> the run loop marks the
    # session failed and re-raises the generation-specific timeout.
    with pytest.raises(GenerationTimeoutError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    assert len(llm.calls) == 1  # the call was attempted (then cancelled)


def test_per_call_timeout_returns_before_cancel_cleanup_finishes():
    async def scenario():
        state = SessionState(messages=[{"role": "system", "content": "sys"}])
        llm = CancelCleanupLLM()
        runner = build_runner(state=state, llm=llm, per_call_timeout=0.01)

        raised = False
        try:
            await asyncio.wait_for(runner.run_loop(), timeout=0.5)
        except GenerationTimeoutError:
            raised = True

        assert raised is True
        assert state.phase is SessionPhase.ERROR
        assert "GenerationTimeoutError" in (state.terminal_reason or "")
        assert len(runner.pending_cleanup_tasks) == 1
        llm.release_cancel.set()
        while runner.pending_cleanup_tasks:
            await asyncio.sleep(0)
        assert llm.cancel_seen.is_set()

    run(scenario())


def test_provider_timeout_is_not_relabelled_as_generation_ceiling():
    import pytest

    class ProviderTimeoutLLM:
        async def complete(self, messages, tools=None, temperature=0.0):
            raise asyncio.TimeoutError("provider transport timeout")

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(
        state=state,
        llm=ProviderTimeoutLLM(),
        per_call_timeout=10.0,
    )

    with pytest.raises(asyncio.TimeoutError) as captured:
        run(runner.run_loop())

    assert not isinstance(captured.value, GenerationTimeoutError)
    assert "provider transport timeout" in str(captured.value)


def test_per_call_timeout_none_does_not_bound_the_call():
    # With no ceiling wired (the default), a generation that takes longer than
    # any small timeout still completes — regression guard that the ceiling is
    # additive and off by default.
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = SlowLLM(llm_response(content="finished", total_tokens=4), delay=0.05)
    runner = build_runner(state=state, llm=llm)  # per_call_timeout defaults to None

    result = run(runner.run_loop())

    assert result == "finished"
    assert state.phase is SessionPhase.DONE


def test_shaper_bounds_model_view_but_leaves_state_messages_full():
    from opencollab.application.shaping import PerToolResultBudgetShaper

    big = "x" * 5000
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "tool", "tool_call_id": "t1", "content": big},
        ]
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, llm=llm, shaper=PerToolResultBudgetShaper(max_chars=1000))

    run(runner.run_loop())

    # The model saw a bounded copy...
    sent_tool = [m for m in llm.calls[0]["messages"] if m.get("role") == "tool"][0]
    assert len(sent_tool["content"]) <= 1000
    assert "re-read a narrower range" in sent_tool["content"]
    # ...while the persisted history kept the full result.
    assert state.messages[1]["content"] == big
