import inspect

import pytest

from opencollab.core.env import LocalEnvironment
from opencollab.core.session import session as session_mod
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


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


def test_session_accepts_explicit_safety_policy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    safety_policy = SandboxInterceptor(str(ws))
    session = session_mod.Session(agent=FakeAgent(), env=env, safety_policy=safety_policy, llm=object())
    proc = session.tool_processor
    assert proc.interceptor is safety_policy
    assert proc.safety_policy is proc.interceptor
    # Path inside workspace resolves; path outside raises.
    assert proc.interceptor.check_path("inside.txt").startswith(str(ws.resolve()))
    with pytest.raises(PermissionError):
        proc.interceptor.check_path("/etc/passwd")


def test_direct_session_does_not_derive_safety_policy_from_env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    session = session_mod.Session(agent=FakeAgent(), env=env, llm=object())

    assert session.tool_processor.safety_policy is None
    assert session.tool_processor.interceptor is None


def test_snapshot_preserves_explicit_safety_policy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    safety_policy = SandboxInterceptor(str(ws))
    session = session_mod.Session(agent=FakeAgent(), env=env, safety_policy=safety_policy, llm=object())

    snap = session.snapshot()

    assert snap.tool_processor.safety_policy is safety_policy


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


def test_tool_call_processor_builds_application_tool_runtime(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(str(ws))
    safety_policy = SandboxInterceptor(str(ws))
    permission_policy = FakePermissionPolicy()
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
        safety_policy=safety_policy,
        permission_policy=permission_policy,
    )

    runtime = proc._tool_runtime()

    assert runtime.environment is env
    assert runtime.safety_policy is safety_policy
    assert runtime.permission_policy is permission_policy


def test_core_session_tools_does_not_import_concrete_sandbox():
    import opencollab.core.session.tools as tools_mod

    assert not hasattr(tools_mod, "SandboxInterceptor")


def test_core_session_session_does_not_import_bootstrap_safety():
    source = inspect.getsource(session_mod.Session._build_runtime)

    assert "opencollab.bootstrap.safety" not in source
