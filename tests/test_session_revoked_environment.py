"""A failed Docker cleanup must end the session without losing tool results."""

import asyncio
from types import SimpleNamespace

import pytest
from session_run_test_support import FakeLLM, build_runner, collect_events, llm_response, tool_call

from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase, SessionState


class MarkerTool(Tool):
    name = "marker"
    description = "Record an execution."
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    async def execute_with_runtime(self, params, runtime):
        self.calls += 1
        return "saved result"


def make_runner(monkeypatch, *, cleanup_succeeds=False, deferred=False):
    environment = DockerEnvironment(container_id="a" * 64)
    environment._attached_bound = True
    docker_calls = []

    async def docker(*args, **kwargs):
        docker_calls.append(args)
        if "opencollab-cancel" in args:
            return SimpleNamespace(returncode=0 if cleanup_succeeds else 125)
        raise asyncio.TimeoutError

    monkeypatch.setattr(environment, "_docker", docker)
    marker = MarkerTool()
    tools = [marker, BashTool()]
    calls = [tool_call("before", "marker"), tool_call("timeout", "bash", '{"command":"grep -R pattern ."}')]
    if deferred:
        child = MarkerTool()
        child.name = "spawn_agent"
        tools.append(child)
        calls.append(tool_call("child", "spawn_agent"))
    calls.append(tool_call("after", "marker"))
    agent = Agent(name="coder", system_prompt="Solve the task.", tools=tools)
    state = SessionState(messages=[{"role": "user", "content": "Fix the code."}])
    llm = FakeLLM([llm_response(tool_calls=calls, finish_reason="tool_calls"), llm_response(content="done")])
    events, bus = collect_events()
    executor = ToolExecutionUseCase(agent=agent, state=state, environment=environment, event_publisher=bus)
    runner = build_runner(state=state, agent=agent, llm=llm, tool_execution=executor, event_bus=bus)
    return runner, environment, marker, llm, events, docker_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_failed_cleanup_preserves_batch_and_stops_model_calls(monkeypatch, deferred):
    runner, environment, marker, llm, events, docker_calls = make_runner(monkeypatch, deferred=deferred)

    with pytest.raises(RuntimeError, match="environment has been revoked"):
        await runner.run_loop()

    assert environment.revoked
    assert runner.state.phase is SessionPhase.ERROR
    assert "environment has been revoked" in runner.state.terminal_reason
    assert len(llm.calls) == 1
    assert marker.calls == 1
    assert len(docker_calls) == 2
    results = [m for m in runner.state.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == (["before", "timeout", "child", "after"] if deferred else
                                                  ["before", "timeout", "after"])
    assert results[0]["content"] == "saved result"
    assert "ProcessCleanupError" in results[1]["content"]
    assert all("Skipped" in m["content"] for m in results[2:])
    assert any(kind == "step_end" for kind, _ in events)
    assert runner.state.pending_events.is_empty()
    if deferred:
        assert runner.agent.find_tool("spawn_agent").calls == 0


@pytest.mark.asyncio
async def test_recoverable_timeout_continues_with_same_environment(monkeypatch):
    runner, environment, marker, llm, _events, _docker_calls = make_runner(monkeypatch, cleanup_succeeds=True)

    assert await runner.run_loop() == "done"
    assert not environment.revoked
    assert runner.state.phase is SessionPhase.DONE
    assert marker.calls == 2
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_revoked_environment_rejects_new_turn_before_model_call(monkeypatch):
    runner, environment, marker, llm, _events, docker_calls = make_runner(monkeypatch)
    environment.revoke()

    with pytest.raises(RuntimeError, match="environment has been revoked"):
        await runner.run_loop()

    assert llm.calls == []
    assert marker.calls == 0
    assert docker_calls == []
    assert runner.state.phase is SessionPhase.ERROR
