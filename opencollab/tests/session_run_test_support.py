from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

from opencollab.application.event_bus import EventBus
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.submit_findings import SubmitFindingsTool
from opencollab.domain.agent import Agent
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


def llm_response(content=None, tool_calls=None, total_tokens=5, input_tokens=1, finish_reason="stop"):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(total_tokens=total_tokens, input_tokens=input_tokens),
        finish_reason=finish_reason,
        reasoning=None,
    )


def tool_call(call_id="call-1", name="submit_findings", arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "tool_choice": kwargs.get("tool_choice"),
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


class CapturingToolExecution:
    def __init__(self, agent):
        self.agent = agent
        self.calls = []

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        messages = []
        for tool_call_data in tool_calls:
            name = tool_call_data["function"]["name"]
            tool = self.agent.find_tool(name)
            if tool is None:
                available = [candidate.name for candidate in self.agent.tools]
                content = f"Error: unknown tool '{name}'. Available: {available}"
            else:
                params = json.loads(tool_call_data["function"].get("arguments") or "{}")
                content = await tool.execute_with_runtime(params, None)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_data["id"],
                    "content": content,
                }
            )
        return ToolProcessingResult(messages_to_append=messages)


class ReadStub:
    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


def collect_events():
    events = []

    async def sink(event):
        events.append((event.type, copy.deepcopy(event.data)))

    return events, EventBus(sink)


def build_runner(*, state, agent, llm, event_bus=None, tool_execution=None, **kwargs):
    return SessionRunUseCase(
        agent=agent,
        state=state,
        llm=llm,
        event_publisher=event_bus if event_bus is not None else EventBus(None),
        tool_execution=tool_execution if tool_execution is not None else FakeToolExecution(),
        **kwargs,
    )


def agent_with_submit():
    return Agent(name="scout", system_prompt="s", tools=[ReadStub(), SubmitFindingsTool()])
