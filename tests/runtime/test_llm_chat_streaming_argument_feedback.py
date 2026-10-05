"""Complete Chat streams deliver malformed arguments to tool error feedback."""

from __future__ import annotations

import json

import httpx
import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.errors import TransientProviderError
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.tools import builtin_tools
from tests.support.provider_sdk_http import install_sdk_transport

_MODEL = "gpt-4o"
_USAGE = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}
_BAD_ARGUMENTS = '{"path":"target.txt"'
_GOOD_ARGUMENTS = '{"path":"target.txt"}'
_MARKER = "verified file contents"


def _call(call_id, arguments):
    return {
        "id": call_id, "type": "function",
        "function": {"name": "file_read", "arguments": arguments},
    }


def _reply(body, message, finish):
    common = {"id": "chatcmpl_feedback", "created": 1, "model": body["model"]}
    if not body.get("stream"):
        return httpx.Response(200, json={
            **common, "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": _USAGE,
        })

    delta = dict(message)
    calls = delta.pop("tool_calls", [])
    chunks = [{
        **common, "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }]
    for index, call in enumerate(calls):
        function = call["function"]
        arguments = function["arguments"]
        split = len(arguments) // 2
        for offset, fragment in enumerate((arguments[:split], arguments[split:])):
            tool_delta = {"index": index, "function": {"arguments": fragment}}
            if offset == 0:
                tool_delta.update({"id": call["id"], "type": "function"})
                tool_delta["function"]["name"] = function["name"]
            chunks.append({
                **common, "object": "chat.completion.chunk", "choices": [{
                    "index": 0, "delta": {"tool_calls": [tool_delta]}, "finish_reason": None,
                }],
            })
    chunks.extend([
        {**common, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {}, "finish_reason": finish},
        ]},
        {**common, "object": "chat.completion.chunk", "choices": [], "usage": _USAGE},
    ])
    wire = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=wire + "data: [DONE]\n\n",
    )


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "false")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")


@pytest.mark.parametrize("stream", [False, True], ids=["ordinary-chat", "complete-stream"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_public_agent_corrects_invalid_arguments_after_tool_error(
    monkeypatch, tmp_path, stream, retries,
):
    (tmp_path / "target.txt").write_text(_MARKER + "\n", encoding="utf-8")
    requests = []
    executions = []
    execute = FileReadTool.execute_with_runtime

    async def record_execution(self, params, runtime):
        executions.append(dict(params))
        return await execute(self, params, runtime)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        tool_results = [message for message in body["messages"] if message["role"] == "tool"]
        if not tool_results:
            message = {"role": "assistant", "content": None, "tool_calls": [
                _call("call_bad", _BAD_ARGUMENTS),
            ]}
            return _reply(body, message, "tool_calls")
        error = next(message for message in tool_results if message["tool_call_id"] == "call_bad")
        assert error["content"] == f"Error: invalid JSON arguments: {_BAD_ARGUMENTS}"
        if len(tool_results) == 1:
            assert executions == []
            message = {"role": "assistant", "content": None, "tool_calls": [
                _call("call_corrected", _GOOD_ARGUMENTS),
            ]}
            return _reply(body, message, "tool_calls")
        assert len(tool_results) == 2
        assert tool_results[1]["tool_call_id"] == "call_corrected"
        assert _MARKER in tool_results[1]["content"]
        assert executions == [{"path": "target.txt"}]
        return _reply(body, {"role": "assistant", "content": "done after actual file read"}, "stop")

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setattr(FileReadTool, "execute_with_runtime", record_execution)
    artifacts = tmp_path / "artifacts"
    app = OpenCollab(
        tmp_path, model=_MODEL, provider="openai",
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"llm_stream_chat": stream, "llm_max_retries": retries, "thinking": False},
    )
    result = await app.agent(
        "Read target.txt and report its content.", tools=builtin_tools("file_read"),
        system_prompt="Read the requested file and answer.",
        budget=10_000, max_steps=6, artifacts=artifacts, trace=True,
    )

    assert result.status == "completed"
    assert result.error is None
    assert result.output == "done after actual file read"
    assert result.tokens == 3 * _USAGE["total_tokens"]
    assert len(requests) == 3
    assert executions == [{"path": "target.txt"}]
    for body in requests:
        assert bool(body.get("stream")) is stream
        if stream:
            assert body["stream_options"] == {"include_usage": True}
    history = OpenCollab.read_session_snapshot(artifacts / "agent.json")["messages"]
    first_call = next(message for message in history if message.get("tool_calls"))["tool_calls"][0]
    assert first_call == _call("call_bad", _BAD_ARGUMENTS)
    errors = [record for record in _records(artifacts / "trajectory.jsonl") if record["type"] == "tool_error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error"] == "invalid_json_args"
    ledger = _records(tmp_path / "usage.jsonl")
    assert len(ledger) == 3
    assert [record["finish_reason"] for record in ledger] == ["tool_calls", "tool_calls", "stop"]
    assert all(record["status"] == "success" for record in ledger)
    assert [record["usage"]["total_tokens"] for record in ledger] == [27, 27, 27]
    assert all(record["usage"]["estimated"] is False for record in ledger)


class _UnfinishedToolStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        chunk = {
            "id": "chatcmpl_unfinished", "object": "chat.completion.chunk",
            "created": 1, "model": _MODEL, "choices": [{
                "index": 0, "finish_reason": None, "delta": {"tool_calls": [
                    {"index": 0, **_call("call_partial", _BAD_ARGUMENTS)},
                ]},
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("retries", [0, 1])
async def test_unfinished_tool_stream_keeps_transient_retry_path(monkeypatch, retries):
    requests = []
    streams = []

    def handler(request):
        requests.append(json.loads(request.content))
        stream = _UnfinishedToolStream()
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async def no_delay(_seconds):
        return None

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", no_delay)
    async with LLMClient(
        model=_MODEL, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=retries, stream_chat=True,
    ) as client:
        with pytest.raises(TransientProviderError, match="without a finish_reason"):
            await client.complete(
                [{"role": "user", "content": "Read target.txt."}],
                tools=[tool.to_openai_schema() for tool in builtin_tools("file_read")],
            )

    assert len(requests) == retries + 1
    assert all(body["messages"] == requests[0]["messages"] for body in requests)
    assert all(stream.closed for stream in streams)
