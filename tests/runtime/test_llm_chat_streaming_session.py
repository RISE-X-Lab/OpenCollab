"""Real Session behavior across streamed provider limits and thinking tools."""

from __future__ import annotations

import pytest
from openai.types.chat import ChatCompletion

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.errors import StreamedUsageUnavailableError
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.runtime.test_llm_chat_streaming import FakeClient, chunk, usage_chunk

USAGE = {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}


def completion(content, calls, finish, reasoning=None):
    result = ChatCompletion.model_validate({
        "id": "chatcmpl-session", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content, "tool_calls": calls,
            "reasoning_content": reasoning,
        }}],
        "usage": USAGE,
    })
    # SDK transport parsing accepts compatible-provider enum extensions.
    result.choices[0].finish_reason = finish
    return result


def finish_chunk(finish):
    result = chunk(finish_reason="stop")
    result.choices[0].finish_reason = finish
    return result


async def session_with_wire(tmp_path, wire, *, stream, tools, thinking=False, retries=0):
    client = LLMClient(model="deepseek-flash", api_key="unused", max_retries=retries, stream_chat=stream)
    await client._openai.close()
    client._openai = wire
    agent = Agent(
        name="coder", system_prompt="system", model="deepseek-flash", tools=tools,
        thinking=thinking, thinking_params={"thinking": {"type": "enabled"}} if thinking else {},
    )
    session = build_session(
        agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)),
        max_steps=3, max_budget_tokens=10_000,
    )
    await session.add_user_message("inspect the file")
    return session


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("retries", [0, 1])
@pytest.mark.parametrize("finish", ["length", "max_tokens"])
async def test_provider_limit_retains_usage_text_and_stops_partial_tool(tmp_path, stream, retries, finish):
    call = {"id": "write-1", "type": "function", "function": {
        "name": "file_write", "arguments": '{"path":"app.py","content":"unfinished',
    }}
    script = [
        chunk(delta={"content": "partial text", "tool_calls": [{"index": 0, **call}]}),
        finish_chunk(finish), usage_chunk(USAGE),
    ] if stream else completion("partial text", [call], finish)
    wire = FakeClient([script])
    session = await session_with_wire(tmp_path, wire, stream=stream, tools=[FileWriteTool()], retries=retries)

    assert await session.run_loop() == "partial text"
    assert session.phase is SessionPhase.STOPPED
    assert session.state.terminal_reason == "output truncated: provider reached its generation limit"
    assert session.used_tokens == 200
    assert len(wire.calls) == 1
    assistants = [message for message in session.state.messages if message["role"] == "assistant"]
    assert assistants == [{"role": "assistant", "content": "partial text"}]
    assert not (tmp_path / "app.py").exists()
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("delta", [
    {"index": 0, "function": {"arguments": '{"path":'}},
    {"index": 0, "id": "write-1", "function": {"name": "file_", "arguments": "{"}},
])
async def test_provider_limit_can_interrupt_tool_identity(tmp_path, delta):
    wire = FakeClient([[
        chunk(delta={"content": "partial text", "tool_calls": [delta]}),
        chunk(finish_reason="length"), usage_chunk(USAGE),
    ]])
    session = await session_with_wire(tmp_path, wire, stream=True, tools=[FileWriteTool()], retries=1)
    assert await session.run_loop() == "partial text"
    assert session.phase is SessionPhase.STOPPED
    assert session.used_tokens == 200
    assert len(wire.calls) == 1
    assert all(stream.closed for stream in wire.streams)


async def test_provider_limit_without_usage_keeps_missing_usage_failure(tmp_path):
    wire = FakeClient([[
        chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}),
        chunk(finish_reason="length"),
    ]])
    session = await session_with_wire(tmp_path, wire, stream=True, tools=[FileWriteTool()], retries=1)
    with pytest.raises(StreamedUsageUnavailableError):
        await session.run_loop()
    assert session.phase is SessionPhase.ERROR
    assert session.used_tokens == 0
    assert len(wire.calls) == 1
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True])
async def test_thinking_tool_continuation_replays_reasoning_through_session(tmp_path, stream, thinking):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    call = {"id": "read-1", "type": "function", "function": {
        "name": "file_read", "arguments": '{"path":"f.py"}',
    }}
    reasoning = "Need to inspect f.py."
    first = [
        chunk(delta={"reasoning_content": reasoning}),
        chunk(delta={"tool_calls": [{"index": 0, **call}]}),
        chunk(finish_reason="tool_calls"), usage_chunk(USAGE),
    ] if stream else completion(None, [call], "tool_calls", reasoning)
    second = [chunk(delta={"content": "done"}), chunk(finish_reason="stop"), usage_chunk(USAGE)] \
        if stream else completion("done", [], "stop")
    wire = FakeClient([first, second])
    session = await session_with_wire(tmp_path, wire, stream=stream, tools=[FileReadTool()], thinking=thinking)

    assert await session.run_loop() == "done"
    assert session.phase is SessionPhase.DONE
    assert session.used_tokens == 400
    assert len(wire.calls) == 2
    assistant = next(message for message in wire.calls[1]["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == reasoning
    tool_result = next(message for message in wire.calls[1]["messages"] if message["role"] == "tool")
    assert "x = 1" in tool_result["content"]
    assert all(stream.closed for stream in wire.streams)
