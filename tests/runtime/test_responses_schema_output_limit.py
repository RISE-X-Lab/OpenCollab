"""A schema output limit preserves usage without capturing a partial result."""

import json

import httpx
import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.responses_provider import complete_responses
from opencollab.application.structured_output import StructuredOutputTool
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import install_sdk_transport
from tests.support.responses_provider_test_support import FakeStream, completed_response, message_item, ns

SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
CHOICE = {"type": "function", "function": {"name": "structured_output"}}
TOOLS = [{"type": "function", "function": {"name": "structured_output", "parameters": SCHEMA}}]

@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("text", [None, '{"result":', '{"result":"ok"}'])
@pytest.mark.parametrize("phased", [False, True], ids=["unphased", "phased"])
async def test_schema_limit_preserves_reported_usage_and_skips_projection(streamed, text, phased):
    items = []
    if phased:
        items.append({**message_item("Collecting the result."), "id": "msg_progress", "phase": "commentary"})
    if text is not None:
        partial = {**message_item(text), "status": "incomplete"}
        if phased:
            partial["phase"] = "final_answer"
        items.append(partial)
    terminal = completed_response(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"}, output=items,
    )

    class Responses:
        calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            assert kwargs["text"]["format"]["schema"] == SCHEMA
            if not streamed:
                return terminal
            return FakeStream([
                *(ns(type="response.output_item.done", output_index=i, item=item) for i, item in enumerate(items)),
                ns(type="response.incomplete", response=terminal),
            ])

    transport = Responses()
    response = await complete_responses(
        ns(responses=transport), "deepseek-v4-flash", [{"role": "user", "content": "Return a result"}],
        TOOLS, 1.0, 0, tool_choice=CHOICE, stream=streamed,
    )
    assert transport.calls == 1
    assert response.finish_reason == "max_tokens"
    assert response.usage.total_tokens == 150
    assert response.usage.estimated is False
    assert response.content == text
    assert response.tool_calls == []
    assert response.provider_items == items

async def test_real_sdk_schema_limit_stops_session_and_keeps_billed_usage(
    local_environment_factory, monkeypatch, tmp_path,
):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        item = {**message_item('{"result":'), "status": "incomplete"}
        terminal = {
            "id": "resp_limited", "object": "response", "status": "incomplete", "model": body["model"],
            "output": [item], "error": None, "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        }
        events = [
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.incomplete", "response": terminal},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ))

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    capture = StructuredOutputTool(SCHEMA)
    agent = Agent(
        name="schema", system_prompt="Return a result", model="deepseek-v4-flash", provider="openai",
        wire_protocol="responses", tools=[capture], tool_choice=CHOICE,
    )
    async with LLMClient(
        provider="openai", model=agent.model, wire_protocol="responses", max_retries=0,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
    ) as client:
        session = build_session(env=local_environment_factory(), agent=agent, llm=client, max_budget_tokens=10000)
        await session.add_user_message("Return a result")
        await session.run_loop()
    assert len(requests) == 1
    assert requests[0]["text"]["format"]["schema"] == SCHEMA
    assert session.phase is SessionPhase.STOPPED
    assert session.used_tokens == 150
    assert capture.captured is None
    assert not any(message.get("tool_calls") for message in session.messages)
