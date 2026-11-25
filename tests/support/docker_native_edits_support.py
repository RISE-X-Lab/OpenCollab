"""Execute Docker command wrappers locally for native-edit concurrency tests."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap.workflow_runtime import WorkflowSessionFactory


class LocalDockerTransport:
    """Replace only daemon transport, retaining _exec and its shell wrapper."""

    def __init__(self, workspace: Path, *, pair_reads: bool = True):
        self.workspace = workspace
        self.pair_reads = pair_reads
        self.second_read = asyncio.Event()
        self.reads: list[str] = []
        self.calls: list[dict] = []
        self.envs: list[DockerEnvironment] = []

    def attach(self, env: DockerEnvironment) -> None:
        self.envs.append(env)
        env._attached_bound = True
        env._docker = self

    async def __call__(self, *argv, timeout=120, input_bytes=None):
        assert argv[0] == "exec"
        boundary = argv.index("--")
        command = argv[boundary + 2:]
        process_env = dict(os.environ)
        for index, arg in enumerate(argv[:boundary]):
            if arg == "-e":
                name, value = argv[index + 1].split("=", 1)
                process_env[name] = value
        workdir = argv[argv.index("-w") + 1] if "-w" in argv[:boundary] else self.workspace
        wrapped = docker_module._EXEC_WRAPPER in command
        self.calls.append({
            "command": command[-1],
            "wrapped": wrapped,
            "active_execs": sum(len(env._active_execs) for env in self.envs),
            "write": input_bytes is not None,
        })
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            env=process_env,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input_bytes), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            raise
        if wrapped and input_bytes is None and "cat -- " in command[-1]:
            # Both old reads complete before either write on the unfixed tree.
            # A locked edit proceeds after the bounded wait for its sibling.
            self.reads.append(out.decode())
            if len(self.reads) == 2:
                self.second_read.set()
            if self.pair_reads and len(self.reads) == 1:
                try:
                    await asyncio.wait_for(self.second_read.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
        return ProcessResult(proc.returncode, out, err)

class RemoteDockerTransport(LocalDockerTransport):
    def __init__(self, workspace: Path, *, pair_reads: bool = False):
        super().__init__(workspace, pair_reads=pair_reads)
        self.remote = f"/oc-native-test-{uuid.uuid4().hex}"

    async def __call__(self, *argv, **kwargs):
        actual = str(self.workspace)
        mapped = tuple(arg.replace(self.remote, actual) if isinstance(arg, str) else arg for arg in argv)
        result = await super().__call__(*mapped, **kwargs)
        return ProcessResult(result.returncode, result.stdout.replace(actual.encode(), self.remote.encode()),
                             result.stderr.replace(actual.encode(), self.remote.encode()))

def docker_environment(workspace: Path, transport: LocalDockerTransport, *, container_id=None):
    env = DockerEnvironment(
        workspace=str(workspace),
        container_id=container_id or uuid.uuid4().hex * 2,
        exec_workdir=str(workspace),
        command_prefix="export OC_NATIVE_EDIT_PROBE=wrapped",
    )
    transport.attach(env)
    return env

def edit_call(mode: str, path: str, index: int) -> tuple[object, dict]:
    name = "alpha" if index == 0 else "beta"
    if mode == "str_replace":
        return FileWriteTool(), {
            "path": path, "mode": mode, "old_str": f"{name} = 1", "new_str": f"{name} = 2",
        }
    if mode == "unified_diff":
        return ApplyPatchTool(), {
            "path": path, "mode": mode,
            "patch": f"@@ -{index + 1} +{index + 1} @@\n-{name} = 1\n+{name} = 2\n",
        }
    return ApplyPatchTool(), {
        "path": path, "mode": "line_replace", "start_line": index + 1, "end_line": index + 1,
        "expected_str": f"{name} = 1", "new_str": f"{name} = 2",
    }

async def workflow_edits(
    workspace: Path, monkeypatch, modes, *, backend="docker", parallel=True, separate=False, relative=(False, False),
):
    paths = [workspace / "module.py", workspace / ("other.py" if separate else "module.py")]
    for path in paths:
        path.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(workspace)
    env = docker_environment(workspace, transport) if backend == "docker" else LocalEnvironment(str(workspace))
    actions = [
        edit_call(mode, paths[index].name if relative[index] else str(paths[index]), index)
        for index, mode in enumerate(modes)
    ]

    async def complete(_client, messages, **_kwargs):
        if any(message.get("role") == "tool" for message in messages):
            return LLMResponse(content="edit complete", finish_reason="stop", usage=Usage(1, 1))
        index = 0 if any(
            message.get("role") == "user" and "alpha" in str(message.get("content"))
            for message in messages
        ) else 1
        tool, params = actions[index]
        return LLMResponse(
            content="", finish_reason="tool_calls", usage=Usage(1, 1), tool_calls=[{
                "id": str(index), "type": "function",
                "function": {"name": tool.name, "arguments": json.dumps(params)},
            }],
        )

    monkeypatch.setattr(LLMClient, "complete", complete)
    factory = WorkflowSessionFactory(
        model="offline", provider="openai", api_key="offline", base_url=None,  # pragma: allowlist secret
        workspace=str(workspace), env=env, max_steps=3,
    )
    ctx = WorkflowContext(factory, budget_total=None, max_concurrency=2)
    try:
        jobs = [
            lambda: ctx.agent("edit alpha", tools=[actions[0][0]]),
            lambda: ctx.agent("edit beta", tools=[actions[1][0]]),
        ]
        outputs = await ctx.parallel(jobs) if parallel else [await job() for job in jobs]
        await ctx.wait_for_pending_cleanup()
        final = [path.read_text() for path in paths]
        return {
            "backend": backend, "parallel": parallel, "separate": separate, "modes": modes,
            "final": final, "reads": transport.reads, "outputs": outputs,
            "errors": list(ctx.agent_failures), "session_count": len(ctx.sessions),
            "tool_outputs": [[
                message["content"] for message in session.state.messages if message.get("role") == "tool"
            ] for session in ctx.sessions],
            "calls": transport.calls,
            "max_real_exec_tracking": max((call["active_execs"] for call in transport.calls), default=0),
            "both_edits_retained": "alpha = 2" in final[0] and "beta = 2" in final[1],
        }
    finally:
        for session in ctx.sessions:
            await session.aclose()
        if backend == "local":
            await env.cleanup()
