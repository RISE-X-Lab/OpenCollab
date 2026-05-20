import asyncio
import importlib
import inspect
import pkgutil

import pytest

from opencollab.application.ports import ToolPort
from opencollab.adapters.env import LocalEnvironment
from opencollab.bootstrap import session as session_mod
from opencollab.application.event_bus import EventBus
from opencollab.domain.session import SessionState
from opencollab.application.tool_processor import ToolCallProcessor
from opencollab.adapters.safety import SandboxInterceptor


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


class RuntimeAwareTool:
    def __init__(self):
        self.runtime_calls = []

    async def execute_with_runtime(self, args, runtime):
        self.runtime_calls.append((args, runtime))
        return "runtime result"


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


def test_tool_call_processor_prefers_execute_with_runtime(tmp_path):
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
    tool = RuntimeAwareTool()

    result, _latency = asyncio.run(proc._execute_tool(tool, {"value": 1}))

    assert result == "runtime result"
    assert len(tool.runtime_calls) == 1
    args, runtime = tool.runtime_calls[0]
    assert args == {"value": 1}
    assert runtime.environment is env
    assert runtime.safety_policy is safety_policy
    assert runtime.permission_policy is permission_policy


def test_tool_call_processor_delegates_runtime_dispatch(tmp_path):
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
    tool = RuntimeAwareTool()
    calls = []

    async def fake_dispatch(dispatch_tool, args, runtime):
        calls.append((dispatch_tool, args, runtime))
        return "dispatched"

    proc._tool_execution.dispatch_tool = fake_dispatch

    result, _latency = asyncio.run(proc._execute_tool(tool, {"value": 1}))

    assert result == "dispatched"
    assert len(calls) == 1
    dispatch_tool, args, runtime = calls[0]
    assert dispatch_tool is tool
    assert args == {"value": 1}
    assert runtime.environment is env
    assert runtime.safety_policy is safety_policy
    assert runtime.permission_policy is permission_policy


def test_tool_call_processor_does_not_expand_legacy_runtime_arguments():
    source = inspect.getsource(ToolCallProcessor._execute_tool)

    assert "execute_tool(" in source
    assert "getattr(tool" not in source
    assert "confirm_fn=runtime.confirm_fn()" not in source
    assert "interceptor=runtime.safety_policy" not in source


def test_tool_call_processor_process_delegates_to_application_use_case(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=LocalEnvironment(str(ws)),
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
    )
    calls = []

    async def fake_process(tool_calls):
        calls.append(tool_calls)
        return "processed"

    monkeypatch.setattr(proc._tool_execution, "process", fake_process)

    result = asyncio.run(proc.process([{"id": "call-1"}]))

    assert result == "processed"
    assert calls == [[{"id": "call-1"}]]


def test_tool_port_describes_runtime_dispatch_not_legacy_execute():
    source = inspect.getsource(ToolPort)

    assert "execute_with_runtime" in source
    assert "async def execute(" not in source
    assert "env:" not in source
    assert "interceptor:" not in source


def test_no_concrete_tool_defines_legacy_execute():
    from opencollab.tools.base import Tool

    offenders: list[str] = []
    for pkg_name in ("opencollab.tools", "opencollab.team"):
        pkg = importlib.import_module(pkg_name)
        for _, mod_name, _ in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
            mod = importlib.import_module(mod_name)
            for cls in vars(mod).values():
                if (
                    isinstance(cls, type)
                    and issubclass(cls, Tool)
                    and cls is not Tool
                    and "execute" in cls.__dict__
                ):
                    offenders.append(f"{mod_name}.{cls.__name__}")

    assert offenders == []


def test_tool_processor_does_not_import_concrete_sandbox():
    import opencollab.application.tool_processor as tools_mod

    assert not hasattr(tools_mod, "SandboxInterceptor")


def test_bootstrap_session_does_not_import_bootstrap_safety():
    source = inspect.getsource(session_mod)

    assert "opencollab.bootstrap.safety" not in source
