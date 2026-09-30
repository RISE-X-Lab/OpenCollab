"""Cancellation keeps the next request's tool exchange locally complete."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.llm.openai_provider import _build_request_kwargs
from opencollab.application.tool_execution import DeferredCall
from opencollab.bootstrap import build_session
from opencollab.domain.pending import RowStatus
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    FakeTool,
    llm_response,
    tool_call,
)


class WaitingTool(FakeTool):
    def __init__(self):
        super().__init__(name="wait_tool")
        self.started = asyncio.Event()
        self.executions = 0

    async def execute_with_runtime(self, args, runtime):
        self.executions += 1
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_first", [False, True])
async def test_cancelled_tool_batch_can_continue_with_complete_chat_history(completed_first, tmp_path):
    waiting = WaitingTool()
    output = tmp_path / "completed.txt"

    def complete_write(_args):
        output.write_text("work already completed", encoding="utf-8")
        return "work already completed"

    completed = FakeTool(name="completed_tool", result=complete_write)
    tools = [completed, waiting] if completed_first else [waiting]
    calls = [tool_call("waiting", "wait_tool", "{}")]
    if completed_first:
        calls.insert(0, tool_call("completed", "completed_tool", "{}"))
    llm = FakeLLMClient(
        [
            llm_response(tool_calls=calls, finish_reason="tool_calls"),
            llm_response(content="continued answer"),
        ]
    )
    session = build_session(agent=FakeAgent(tools=tools), llm=llm)
    await session.add_user_message("run tools")
    active = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(waiting.started.wait(), timeout=0.5)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(active, timeout=0.5)

    assert session.phase is SessionPhase.STOPPED
    assert session._open_tool_call_ids() == []
    interrupted = [message for message in session.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in interrupted] == [call["id"] for call in calls]
    assert all("outcome is unknown" in message["content"] for message in interrupted)
    assert len(completed.calls) == int(completed_first)
    assert output.exists() is completed_first
    if completed_first:
        assert output.read_text(encoding="utf-8") == "work already completed"

    await session.add_user_message("continue")
    assert await session.run_loop() == "continued answer"
    request = _build_request_kwargs(
        "fake-model", llm.calls[-1]["messages"], llm.calls[-1]["tools"],
        0.0, False, None, None, None, None, None,
    )
    assert [
        message["tool_call_id"] for message in request["messages"] if message["role"] == "tool"
    ] == [call["id"] for call in calls]
    assert waiting.executions == 1
    assert len(completed.calls) == int(completed_first)


@pytest.mark.asyncio
async def test_cancellation_keeps_deferred_result_with_a_live_producer():
    class ProducingTool(FakeTool):
        def __init__(self):
            super().__init__(name="spawn_agent")
            self.release = asyncio.Event()
            self.producer = None
            self.state = None

        async def execute_with_runtime(self, args, runtime):
            async def produce():
                await self.release.wait()
                self.state.pending_events.fill(runtime.tool_call_id, result="real child result")

            self.producer = asyncio.create_task(produce())
            return DeferredCall(ref=7)

    producer = ProducingTool()
    waiting = WaitingTool()
    llm = FakeLLMClient(
        [llm_response(tool_calls=[
            tool_call("child", "spawn_agent", "{}"),
            tool_call("waiting", "wait_tool", "{}"),
        ], finish_reason="tool_calls")]
    )
    session = build_session(agent=FakeAgent(tools=[producer, waiting]), llm=llm)
    producer.state = session.state
    active = asyncio.create_task(session.run_loop())
    try:
        await asyncio.wait_for(waiting.started.wait(), timeout=0.5)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, timeout=0.5)

        row = session.state.pending_events.rows["child"]
        assert row.ref == 7
        assert row.status is RowStatus.PENDING
        assert producer.producer is not None and not producer.producer.done()
        assert session._open_tool_call_ids() == ["child"]
        assert all(
            message.get("tool_call_id") != "child" for message in session.messages
        )
        producer.release.set()
        await asyncio.wait_for(producer.producer, timeout=0.5)
        assert session.state.pending_events.rows["child"].result == "real child result"
        assert session.state.pending_events.rows["child"].status is RowStatus.DONE
    finally:
        producer.release.set()
        if producer.producer is not None:
            await producer.producer
