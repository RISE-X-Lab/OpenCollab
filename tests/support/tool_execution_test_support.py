"""Small reusable doubles for tool-execution tests."""

from types import SimpleNamespace

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionState


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
        tool_start=lambda tool, args, tool_call_id: SimpleNamespace(
            type="tool_start", data={}
        ),
        tool_end=lambda tool, latency, tool_call_id: SimpleNamespace(
            type="tool_end", data={}
        ),
    )
    return ToolExecutionUseCase(
        agent=FakeAgent(tools=[tool]),
        environment=None,
        state=state,
        event_publisher=NullEventPublisher(),
        event_factory=event_factory,
    )


class RuntimeNativeTool:
    name = "fake_tool"

    def __init__(self, output: str = "runtime result"):
        self.output = output
        self.runtime_calls = []

    async def execute_with_runtime(self, args, runtime):
        self.runtime_calls.append((args, runtime))
        return self.output


def event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    # Wrap to use SimpleNamespace so tests that previously asserted on a
    # plain object (no aid field) keep their assertion shapes.
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(
            type="loop_detected",
            data={"tool": tool, "count": count},
        ),
        tool_start=lambda tool, args, tool_call_id: SimpleNamespace(
            type="tool_start",
            data={"tool": tool, "args": args, "tool_call_id": tool_call_id},
        ),
        tool_end=lambda tool, latency, tool_call_id: SimpleNamespace(
            type="tool_end",
            data={
                "tool": tool,
                "latency": latency,
                "tool_call_id": tool_call_id,
            },
        ),
    )


def build_use_case(
    *,
    agent=None,
    state=None,
    event_publisher=None,
    tracer=None,
    environment=None,
    permission_policy=None,
    safety_policy=None,
    **use_case_kwargs,
):
    publisher = event_publisher or RecordingEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=agent or FakeAgent(),
        environment=environment,
        state=state or SessionState(messages=[]),
        event_publisher=publisher,
        event_factory=event_factory(),
        tracer=tracer,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
        **use_case_kwargs,
    )
    return use_case, publisher
