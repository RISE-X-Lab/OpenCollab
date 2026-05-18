import pytest

from opencollab.core.env import LocalEnvironment
from opencollab.core.session.events import EventBus
from opencollab.core.session.state import SessionState
from opencollab.core.session.tools import ToolCallProcessor
from opencollab.tools.safety import SandboxInterceptor


class FakeAgent:
    tools: list = []

    def find_tool(self, name):
        return None


def test_tool_call_processor_derives_interceptor_from_env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
    )
    assert isinstance(proc.interceptor, SandboxInterceptor)
    # Path inside workspace resolves; path outside raises.
    assert proc.interceptor.check_path("inside.txt").startswith(str(ws.resolve()))
    with pytest.raises(PermissionError):
        proc.interceptor.check_path("/etc/passwd")


def test_tool_call_processor_accepts_explicit_interceptor(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    custom = SandboxInterceptor(str(ws))
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
        interceptor=custom,
    )
    assert proc.interceptor is custom
