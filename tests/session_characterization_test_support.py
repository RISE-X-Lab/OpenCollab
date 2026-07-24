"""Shared fakes and builders for session characterization tests."""

from __future__ import annotations

import asyncio
import copy

from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.application.event_bus import EventBus


def run(coro):
    return asyncio.run(coro)

def llm_response(content=None, tool_calls=None, input_tokens=1, output_tokens=1, finish_reason="stop"):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason=finish_reason,
    )

def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }

class FakeLLMClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append({
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "temperature": temperature,
        })
        if not self.responses:
            raise AssertionError("FakeLLMClient received an unexpected complete() call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

class FakeAgent:
    def __init__(self, tools=None):
        self.name = "fake-agent"
        self.system_prompt = "You are a fake agent."
        self.tools = tools or []
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.api_key = "fake-key"  # pragma: allowlist secret
        self.base_url = "https://fake.invalid"
        self.temperature = 0.25

    def tool_schemas(self):
        return [tool.to_openai_schema() for tool in self.tools]

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name.lower() == name.lower():
                return tool
        return None

def fake_team_session_factory(*, safety_policy_factory=None, lead_workspace=None, interactive=False):
    from opencollab.bootstrap.container import DefaultSessionFactory, SpawnConfig

    event_bus = EventBus()
    factory = DefaultSessionFactory(
        SpawnConfig(
            model="fake-model",
            provider="fake-provider",
            api_key="fake-key",  # pragma: allowlist secret
            base_url=None,
            llm_timeout=600.0,
            tracer=None,
            event_bus=event_bus,
            permission_policy=None,
            safety_policy_factory=safety_policy_factory,
        ),
        lead_workspace=lead_workspace,
        interactive=interactive,
    )
    return event_bus, factory

def fake_worktree_pool():
    from opencollab.adapters.worktree_pool import WorktreePool

    return WorktreePool(".", use_worktrees=False)

class FakeTool:
    def __init__(self, name="fake_tool", result="tool result", exc=None):
        self.name = name
        self.result = result
        self.exc = exc
        self.calls = []

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fake tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute_with_runtime(self, args, runtime):
        self.calls.append({
            "args": copy.deepcopy(args),
            "env": runtime.environment,
            "interceptor": runtime.safety_policy,
            "confirm_fn": runtime.confirm_fn(),
        })
        if self.exc:
            raise self.exc
        return self.result(args) if callable(self.result) else self.result

class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens, latency):
        self.steps.append({
            "step_type": step_type,
            "payload": copy.deepcopy(payload),
            "tokens": tokens,
            "latency": latency,
        })

    def flush(self):
        self.flush_count += 1

def event_collector():
    events = []

    def on_event(event):
        events.append(event)

    return events, on_event
