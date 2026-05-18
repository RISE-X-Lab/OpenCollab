import pytest

from opencollab.core.env import LocalEnvironment
from opencollab.core.session import Session
from opencollab.core.session.events import EventBus
from opencollab.core.session.state import SessionState
from opencollab.core.session.tools import ToolCallProcessor
from opencollab.tools.safety import SandboxInterceptor


class FakeAgent:
    system_prompt = "fake prompt"
    tools: list = []
    model = "fake-model"
    provider = "fake-provider"
    api_key = "fake-key"
    base_url = "https://fake.invalid"

    def find_tool(self, name):
        return None

    def tool_schemas(self):
        return []


def test_session_derives_interceptor_from_env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    session = Session(
        agent=FakeAgent(),
        env=env,
        llm=object(),
    )
    proc = session.tool_processor
    assert isinstance(proc.interceptor, SandboxInterceptor)
    assert proc.safety_policy is proc.interceptor
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
    assert proc.safety_policy is custom


def test_tool_call_processor_accepts_explicit_safety_policy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    custom = SandboxInterceptor(str(ws))
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
        safety_policy=custom,
    )
    assert proc.safety_policy is custom
    assert proc.interceptor is custom


def test_core_session_tools_does_not_import_concrete_sandbox():
    import opencollab.core.session.tools as tools_mod

    assert not hasattr(tools_mod, "SandboxInterceptor")
