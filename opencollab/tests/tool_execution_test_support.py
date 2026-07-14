"""Small reusable doubles for tool-execution tests."""

from types import SimpleNamespace

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import ToolExecutionUseCase


class FakeAgent:
    def __init__(self, tools=None):
        self.tools = tools or []

    def find_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)


class NullEventPublisher:
    async def emit(self, event):
        pass


class RecordingEventPublisher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class AlwaysAllowPermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


def build_sensor_use_case(state, tool):
    factory = default_session_event_factory(aid=-1)
    event_factory = SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(type="loop_detected", data={}),
        tool_start=lambda tool, args: SimpleNamespace(type="tool_start", data={}),
        tool_end=lambda tool, latency: SimpleNamespace(type="tool_end", data={}),
    )
    return ToolExecutionUseCase(
        agent=FakeAgent(tools=[tool]),
        environment=None,
        state=state,
        event_publisher=NullEventPublisher(),
        event_factory=event_factory,
    )
