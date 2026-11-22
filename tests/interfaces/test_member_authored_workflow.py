from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.bash import BashTool
from opencollab.application.event_bus import EventBus
from opencollab.application.tool_execution import ToolRuntime
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap import container
from opencollab.bootstrap.authored_workflow import AuthoredWorkflowTool
from opencollab.bootstrap.context_builder import SpawnConfig
from opencollab.bootstrap.runtime_context import build_workspace_safety_policy
from opencollab.bootstrap.session_factory import build_session
from opencollab.bootstrap.team_workflow import TeamWorkflowTool
from opencollab.bootstrap.workflow_runtime import run_workflow
from opencollab.domain.agent import Agent
from opencollab.sdk import OpenCollab
from tests.support.docker_native_edits_support import RemoteDockerTransport
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.paths import REPO_ROOT


@pytest.fixture(autouse=True)
def _child_import_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", os.devnull)


def _call(name, args):
    return {"id": "call", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@contextmanager
def _serve_model(reply):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            message, usage = reply(body)
            payload = json.dumps({
                "id": "offline", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                "choices": [{"index": 0, "message": message, "finish_reason": (
                    "tool_calls" if message.get("tool_calls") else "stop"
                )}],
                "usage": usage,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_member_writes_workflow_source_and_collects_loopback_model_result(
    tmp_path, monkeypatch,
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            message = {"role": "assistant", "content": "loopback answer"}
            finish = "stop"
            if not any(item["role"] == "tool" for item in body["messages"]):
                message.update(content=None, tool_calls=[_call("file_read", {"path": "authored.py"})])
                finish = "tool_calls"
            payload = json.dumps({
                "id": "offline", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    observed = {}
    source = '''from opencollab.workflows import workflow
from opencollab.tools import builtin_tools

@workflow
async def main(ctx, args):
    tools = builtin_tools("file_read", limits={"file_read": {"max_read_chars": 5000}})
    answer = await ctx.agent(args["question"], tools=tools)
    return {"answer": answer, "number": sum(range(args["count"]))}
'''

    class ScriptedParent:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 200_000

        async def close(self):
            pass

        async def complete(self, messages, **kwargs):
            replies = [message for message in messages if message["role"] == "tool"]
            if len(replies) == 0:
                call = _call("file_write", {"path": "authored.py", "content": source, "mode": "create"})
            elif len(replies) == 1:
                call = _call("manage_workflow", {"action": "launch", "source": "authored.py",
                             "args": {"question": "answer locally", "count": 6}, "python": sys.executable})
            elif len(replies) == 2:
                launched = json.loads(replies[-1]["content"])
                assert launched["status"] == "running"
                call = _call("manage_workflow", {"action": "wait", "id": launched["id"]})
            else:
                observed.update(json.loads(replies[-1]["content"]))
                return LLMResponse(content="collected child result", usage=Usage(input_tokens=3, output_tokens=2),
                                   finish_reason="stop")
            return LLMResponse(tool_calls=[call], usage=Usage(input_tokens=3, output_tokens=2),
                               finish_reason="tool_calls")

    monkeypatch.setattr(container, "LLMClient", ScriptedParent)
    team = tmp_path / "team.yaml"
    team.write_text("entry: writer\nroles:\n  writer:\n    prompt: Writer\n"
                    "    tools: [file_write, file_read, bash, manage_workflow]\n"
                    "tool_limits:\n  file_read:\n    max_read_chars: 64\n")
    try:
        result = await OpenCollab(tmp_path, model="gpt-4o", api_key="offline-test",  # pragma: allowlist secret
                                  base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                  config={"budget": 10_000, "thinking": False}).team(
            "write and execute your own Workflow", config=team, artifacts=tmp_path / "parent-artifacts",
            trace=False, use_worktrees=False, allow_unisolated_shell=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.output == "collected child result"
    assert result.tokens == 56
    assert observed["status"] == "completed"
    assert observed["result"]["output"] == {"answer": "loopback answer", "number": 15}
    assert observed["tokens"] == 36
    assert len(requests) == 2
    assert requests[0]["model"] == "gpt-4o"
    child_tool_reply = [item for item in requests[1]["messages"] if item["role"] == "tool"][-1]["content"]
    assert "more lines below" in child_tool_reply
    assert "return {" not in child_tool_reply
    assert json.loads((Path(observed["artifacts"]) / "workflow.json").read_text())["status"] == "completed"
    assert not list(tmp_path.glob(".oc-child-request-*"))
    assert all("offline-test" not in path.read_text(errors="ignore")
               for path in tmp_path.rglob("*") if path.is_file())


def _manager(environment, *, budget=1000, shell=True, llm=None):
    class UnusedModel:
        def context_window(self):
            return 200_000

    agent = Agent(name="parent", system_prompt="parent", tools=[BashTool()] if shell else [])
    config = SpawnConfig(model="gpt-4o", provider="openai", api_key=None, base_url=None,
                         llm_timeout=60, tracer=None, event_bus=EventBus(), permission_policy=None)
    tool = AuthoredWorkflowTool(lambda: parent, config, None, allow_unisolated_shell=True)
    agent.tools.append(tool)
    parent = build_session(agent=agent, env=environment, max_budget_tokens=budget, llm=llm or UnusedModel())
    return parent, tool, ToolRuntime(environment, None, None)


async def _launch(tool, runtime, **kwargs):
    return json.loads(await tool.execute_with_runtime(
        {"action": "launch", "source": "authored.py", "python": sys.executable, **kwargs}, runtime,
    ))


async def _wait_file(path):
    async def wait():
        while not path.exists():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(wait(), 10)


async def test_docker_authored_source_resolves_parent_in_the_runner(tmp_path):
    class RunnerTransport(RemoteDockerTransport):
        async def __call__(self, *argv, input_bytes=None, **kwargs):
            if argv[0] == "cp":
                source = argv[-2].split(":", 1)[1].replace(self.remote, str(self.workspace))
                shutil.copyfile(source, argv[-1])
                return ProcessResult(0, b"", b"")
            if input_bytes is not None and "opencollab.bootstrap.authored_workflow_runner" in argv[-1]:
                input_bytes = input_bytes.replace(self.remote.encode(), str(self.workspace).encode())
            return await super().__call__(*argv, input_bytes=input_bytes, **kwargs)

    (tmp_path / "nested").mkdir()
    (tmp_path / "authored.py").write_text('async def main(ctx, args):\n    return "WORKFLOW_MARKER"\n')
    transport = RunnerTransport(tmp_path)
    environment = DockerEnvironment(
        workspace=transport.remote, exec_workdir=transport.remote, container_id="b" * 64,
    )
    transport.attach(environment)
    parent, tool, runtime = _manager(environment)
    runtime = replace(runtime, safety_policy=build_workspace_safety_policy(environment))
    try:
        child = await _launch(tool, runtime, source="nested/../authored.py")
        result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
        assert result["status"] == "completed", result
        assert result["result"]["output"] == "WORKFLOW_MARKER"
    finally:
        await parent.aclose()
        await environment.cleanup()


@pytest.mark.parametrize("stop", ["explicit", "parent-close", "cancel-wait"])
async def test_stop_cleans_child_before_returning(local_environment_factory, tmp_path, stop):
    (tmp_path / "authored.py").write_text('''import asyncio
from pathlib import Path

async def main(ctx, args):
    Path("entered").write_text("ready")
    try:
        await asyncio.sleep(300)
    finally:
        Path("cleaned").write_text("yes")
''')
    env = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(env)
    launched = await _launch(tool, runtime)
    await _wait_file(tmp_path / "entered")
    waiting = asyncio.create_task(tool.execute_with_runtime({"action": "wait", "id": launched["id"]}, runtime))
    if stop == "explicit":
        stopped = json.loads(await tool.execute_with_runtime({"action": "stop", "id": launched["id"]}, runtime))
        assert stopped["status"] == "stopped"
    elif stop == "parent-close":
        await parent.aclose()
    else:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await parent.aclose()
    if not waiting.cancelled():
        assert json.loads(await waiting)["status"] == "stopped"
    assert (tmp_path / "cleaned").read_text() == "yes"
    assert not env._processes._processes
    assert not env.revoked


async def test_wait_timeout_keeps_child_and_result_stable(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text('''import asyncio
async def main(ctx, args):
    await asyncio.sleep(0.5)
    return {"value": 7}
''')
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)))
    child = await _launch(tool, runtime)
    pending = json.loads(await tool.execute_with_runtime(
        {"action": "wait", "id": child["id"], "timeout": 0.01}, runtime,
    ))
    assert pending["status"] == "running" and pending["result"] is None
    completed = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
    assert completed["result"]["output"] == {"value": 7}
    assert json.loads(await tool.execute_with_runtime({"action": "result", "id": child["id"]}, runtime)) == completed
    assert parent.used_tokens == 0
    await parent.aclose()


async def test_stop_grace_leaves_finished_runner_result_persistence_owned(
    local_environment_factory, tmp_path, monkeypatch,
):
    from opencollab.bootstrap import authored_workflow

    monkeypatch.setattr(authored_workflow, "_RUNNER_STOP_GRACE_SECONDS", 0.05)
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 1\n")
    environment = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(environment)
    entered, finish = asyncio.Event(), asyncio.Event()
    native_copy = authored_workflow._copy_artifact_to_host
    read_cancelled = False

    async def delayed_result(environment, path, target):
        nonlocal read_cancelled
        await native_copy(environment, path, target)
        if path.endswith("/result.json"):
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                read_cancelled = True
                raise

    monkeypatch.setattr(authored_workflow, "_copy_artifact_to_host", delayed_result)
    child = await _launch(tool, runtime)
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert tool._children[child["id"]].execution.done()
        stopping = asyncio.create_task(tool.execute_with_runtime({"action": "stop", "id": child["id"]}, runtime))
        await asyncio.sleep(0.1)
        assert not stopping.done() and not read_cancelled
        finish.set()
        result = json.loads(await asyncio.wait_for(stopping, 2))
        assert result["status"] == "completed" and result["result"]["output"] == 1
    finally:
        finish.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await parent.aclose()


async def test_child_budget_reservations_and_parent_ownership(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("import asyncio\nasync def main(ctx, args):\n    await asyncio.sleep(300)\n")
    env = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(env, budget=100)
    first = await _launch(tool, runtime, budget=40)
    second = await _launch(tool, runtime, budget=90)
    assert tool._children[first["id"]].budget == 40
    assert tool._children[second["id"]].budget == 60
    with pytest.raises(ValueError, match="reserved"):
        await _launch(tool, runtime)
    other_parent, other_tool, other_runtime = _manager(env)
    with pytest.raises(ValueError, match="unknown child"):
        await other_tool.execute_with_runtime({"action": "stop", "id": first["id"]}, other_runtime)
    await parent.aclose()
    await other_parent.aclose()


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
async def test_parent_terminal_turn_stops_owned_children(local_environment_factory, tmp_path, terminal):
    (tmp_path / "authored.py").write_text('''import asyncio
from pathlib import Path
async def main(ctx, args):
    Path("entered").write_text("ready")
    try:
        await asyncio.sleep(300)
    finally:
        Path("cleaned").write_text("yes")
''')

    class ParentModel:
        def context_window(self):
            return 200_000

        async def complete(self, messages, **kwargs):
            if not any(m["role"] == "tool" for m in messages):
                return LLMResponse(tool_calls=[_call("manage_workflow", {
                    "action": "launch", "source": "authored.py", "python": sys.executable,
                })], usage=Usage(input_tokens=3, output_tokens=2), finish_reason="tool_calls")
            await _wait_file(tmp_path / "entered")
            if terminal == "failed":
                raise ValueError("parent failed")
            if terminal == "cancelled":
                raise asyncio.CancelledError
            return LLMResponse(content="parent finished", usage=Usage(input_tokens=3, output_tokens=2),
                               finish_reason="stop")

    env = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(env, budget=10_000, llm=ParentModel())
    await parent.add_user_message("launch then finish")
    if terminal == "failed":
        with pytest.raises(ValueError, match="parent failed"):
            await parent.run_loop()
    elif terminal == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await parent.run_loop()
    else:
        assert await parent.run_loop() == "parent finished"
    assert (tmp_path / "cleaned").exists()
    assert all(item.status == "stopped" for item in tool._children.values())
    assert not env._processes._processes
    await parent.aclose()


async def test_unbounded_parent_can_launch_unbounded_children(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 7\n")
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)), budget=None)
    child = await _launch(tool, runtime)
    assert tool._children[child["id"]].budget is None
    result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
    assert result["result"]["output"] == 7
    await parent.aclose()


async def test_owned_child_cleanup_remains_idempotent_after_environment_closes(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 7\n")
    environment = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(environment, budget=None)
    child = await _launch(tool, runtime)
    result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
    assert result["status"] == "completed" and result["result"]["output"] == 7
    await parent.aclose()
    assert not Path(child["artifacts"]).exists()
    await environment.cleanup()
    await parent.aclose()


async def test_child_reservations_limit_parent_requests_and_release_after_stop(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("import asyncio\nasync def main(ctx, args):\n    await asyncio.sleep(300)\n")
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)))
    messages = [{"role": "user", "content": "brief"}]

    def limit():
        return parent.runner._request_output_limit(messages, None, thinking=False)

    before = limit()
    launched = await _launch(tool, runtime, budget=400)
    child = tool._children[launched["id"]]
    assert limit() == before - 400
    tool._account_usage(child, 50)
    assert limit() == before - 400
    tool._account_usage(child, 50)
    assert parent.used_tokens == 50
    await tool.execute_with_runtime({"action": "stop", "id": child.id}, runtime)
    assert limit() == before - 50
    await parent.aclose()


@pytest.mark.parametrize("source", [
    "async def main(ctx, args):\n    raise ValueError('authored failure')\n",
    "this is invalid python !",
    "async def main(ctx, args):\n    return object()\n",
    "async def main(ctx, args):\n    return float('nan')\n",
])
async def test_authored_errors_are_explicit(local_environment_factory, tmp_path, source):
    (tmp_path / "authored.py").write_text(source)
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)))
    child = await _launch(tool, runtime)
    result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
    assert result["status"] == "failed"
    assert result["result"]["reason"]
    await parent.aclose()


async def test_child_sessions_preserve_member_tool_permissions(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text(
        "async def main(ctx, args):\n    return await ctx.agent('read', tools='coding')\n",
    )
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)))
    child = await _launch(tool, runtime)
    result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
    assert result["result"]["output"] is None
    assert result["result"]["agent_failures"][0]["exception_type"] == "PermissionError"
    assert parent.used_tokens == 0
    await parent.aclose()


@pytest.mark.parametrize("capture", ["structured_output", "submit_findings"])
async def test_managed_workflows_retain_internal_capture_tools(local_environment_factory, tmp_path, capture):
    schema = {
        "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
    }
    options = {"schema": schema} if capture == "structured_output" else {
        "enforcement_strength": "needs-enforcement",
    }
    payload = {"value": 7} if capture == "structured_output" else {
        "findings": [], "summary": "No source evidence was requested.", "insufficient_evidence": True,
    }

    def reply(body):
        assert [tool["function"]["name"] for tool in body["tools"]] == [capture]
        return {"role": "assistant", "content": None, "tool_calls": [_call(capture, payload)]}, {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        }

    async def regular(ctx, args):
        return await ctx.agent("Return a result", tools=[], **options)

    (tmp_path / "authored.py").write_text(
        "async def main(ctx, args):\n"
        "    return await ctx.agent('Return a result', tools=[], **args)\n",
    )
    with _serve_model(reply) as (url, requests):
        config = {"model": "gpt-4o", "provider": "openai", "api_key": "offline-test",  # pragma: allowlist secret
                  "base_url": url, "thinking": False}
        control = await run_workflow(
            regular, {}, cfg=config, workspace=str(tmp_path), trace=False, budget=None,
            return_details=True, limit_mode="explicit",
        )
        parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)), budget=None)
        parent.agent.base_url = url
        parent.agent.api_key = config["api_key"]
        try:
            child = await _launch(tool, runtime, args=options)
            result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
            assert result["status"] == "completed"
            assert result["result"]["output"] == control.output
            assert result["result"]["agent_failures"] == []
            assert result["tokens"] == parent.used_tokens == control.tokens == 18
            assert len(requests) == 2
            if capture == "structured_output":
                assert control.output == payload
            else:
                assert payload["summary"] in control.output
        finally:
            await parent.aclose()


async def test_read_only_member_cannot_execute_source(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 1\n")
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)), shell=False)
    with pytest.raises(PermissionError, match="bash permission"):
        await _launch(tool, runtime)
    await parent.aclose()


async def test_stop_before_command_starts_releases_reservation(local_environment_factory, tmp_path, monkeypatch):
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 1\n")
    env = local_environment_factory(str(tmp_path))
    parent, tool, runtime = _manager(env)
    launched = await _launch(tool, runtime)

    async def unavailable_directory(*args):
        raise FileNotFoundError("child directory is not ready")

    monkeypatch.setattr(env, "write_file", unavailable_directory)
    result = json.loads(await tool.execute_with_runtime({"action": "stop", "id": launched["id"]}, runtime))
    assert result["status"] == "stopped"
    assert tool._reserved_tokens() == 0
    assert not env._processes._processes
    await parent.aclose()


async def test_member_shell_isolation_requirement_is_retained(local_environment_factory, tmp_path):
    (tmp_path / "authored.py").write_text("async def main(ctx, args):\n    return 1\n")
    parent, tool, runtime = _manager(local_environment_factory(str(tmp_path)))
    parent.agent.find_tool("bash").require_process_isolation = True
    with pytest.raises(PermissionError, match="OS process sandbox"):
        await _launch(tool, runtime)
    await parent.aclose()


@pytest.mark.parametrize("budget", [1000, None])
async def test_registered_workflow_honors_live_managed_reservations(local_environment_factory, tmp_path, budget):
    (tmp_path / "authored.py").write_text('''import asyncio
from pathlib import Path
async def main(ctx, args):
    Path("entered").write_text("ready")
    while not Path("release").exists():
        await asyncio.sleep(0.01)
    return await ctx.agent("Return a result", tools=[])
''')
    registered_limits = []

    class RegisteredModel:
        def context_window(self):
            return 200_000

        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return 5

        async def complete(self, messages, **kwargs):
            registered_limits.append(kwargs["max_output_tokens"])
            return LLMResponse(content="registered answer", finish_reason="stop", usage=Usage(
                input_tokens=5, output_tokens=min(695, kwargs["max_output_tokens"]),
            ))

    def reply(body):
        assert body["max_tokens"] >= 50
        return {"role": "assistant", "content": "managed answer"}, {
            "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350,
        }

    async def regular(ctx, args):
        return {"budget": ctx.budget.total, "answer": await ctx.agent("Return a result", tools=[])}

    with _serve_model(reply) as (url, requests):
        parent, tool, runtime = _manager(
            local_environment_factory(str(tmp_path)), budget=budget, llm=RegisteredModel(),
        )
        parent.agent.base_url = url
        parent.agent.api_key = "offline-test"  # pragma: allowlist secret
        registry = Registry()
        registry.register(WorkflowSpec(name="regular", description="Return an answer", fn=regular))
        registered = TeamWorkflowTool(lambda: parent, registry, tool._config, None)
        parent.agent.tools.append(registered)
        try:
            child = await _launch(tool, runtime, budget=400)
            await _wait_file(tmp_path / "entered")
            parent_limit = parent.runner._request_output_limit(
                [{"role": "user", "content": "Return a result"}], None, thinking=False,
            )
            value = json.loads(await registered.execute_with_runtime({"name": "regular", "args": {}}, runtime))
            assert value["output"]["budget"] == (600 if budget is not None else None)
            assert value["output"]["answer"] == "registered answer"
            assert registered_limits == [parent_limit]
            assert tool._reserved_tokens() == 400
            (tmp_path / "release").write_text("go")
            result = json.loads(await tool.execute_with_runtime({"action": "wait", "id": child["id"]}, runtime))
            assert result["result"]["output"] == "managed answer"
            assert result["tokens"] == 350
            assert parent.used_tokens == value["tokens"] + result["tokens"]
            assert parent.used_tokens == (950 if budget is not None else 1050)
            assert len(requests) == 1
        finally:
            await parent.aclose()
