from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.bootstrap import _workflow_runtime_session as wiring
from opencollab.bootstrap import container
from opencollab.tools import builtin_tools


def _repo(path, *, dirty=True):
    for args in (("init", "-q"), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "seed.txt").write_text("initial")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True, capture_output=True)
    if dirty:
        (path / "seed.txt").write_text("dirty")


def _call(name, args):
    return {"id": "call", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Writer:
    def __init__(self, **kwargs):
        pass

    def context_window(self):
        return 100_000

    async def close(self):
        pass

    async def complete(self, messages, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            return LLMResponse(content="finished", usage=Usage(3, 2), finish_reason="stop")
        name = "marker"
        return LLMResponse(tool_calls=[_call("file_write", {
            "mode": "create", "path": name + ".txt", "content": name,
        })], usage=Usage(3, 2), finish_reason="tool_calls")


@pytest.mark.parametrize("default, override, separate", [
    (None, None, False), ("worktree", None, True), ("worktree", False, False),
    ("shared", True, True), ("worktree", "shared", False), ("shared", "worktree", True),
])
async def test_workflow_defaults_and_per_call_overrides(tmp_path, monkeypatch, default, override, separate):
    _repo(tmp_path, dirty=False)
    sessions = []
    original = wiring.build_session

    def build(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(wiring, "build_session", build)

    async def flow(ctx, args):
        result = await ctx.agent("marker", tools=list(builtin_tools("file_write")), isolation=override)
        assert await sessions[-1].env.read_file("marker.txt") == "marker"
        assert await sessions[-1].env.read_file("seed.txt") == "initial"
        return result

    result = await OpenCollab(tmp_path).workflow(flow, isolation=default, llm=Writer(), trace=False)
    assert result.ok and result.output == "finished"
    assert (tmp_path / "marker.txt").exists() is (not separate)
    assert (tmp_path / "seed.txt").read_text() == "initial"


async def test_custom_provider_is_fresh_per_workflow_and_owns_cleanup(tmp_path):
    _repo(tmp_path)
    created = []

    class PrivatePool(WorktreePool):
        def __init__(self, **kwargs):
            super().__init__(use_worktrees=True, **kwargs)
            self.released = False

        async def release(self):
            await super().release()
            self.released = True

    def provider(**kwargs):
        pool = PrivatePool(**kwargs)
        created.append(pool)
        return pool

    client = OpenCollab(tmp_path, environment_providers={"custom": provider})

    async def flow(ctx, args):
        return await ctx.agent("private", tools=list(builtin_tools("file_write")))

    for _ in range(2):
        result = await client.workflow(flow, isolation="custom", llm=Writer(), trace=False)
        assert result.ok
    assert len(created) == 2 and all(pool.released for pool in created)
    assert not (tmp_path / "marker.txt").exists()


async def test_workflow_workspace_release_is_safe_for_concurrent_callers(tmp_path):
    _repo(tmp_path, dirty=False)
    removed = []

    class ObservedPool(WorktreePool):
        async def release_env(self, env):
            assert await env.read_file("marker.txt") == "marker"
            removed.append(Path(env.workspace))
            await super().release_env(env)

    def provider(**kwargs):
        return ObservedPool(use_worktrees=True, **kwargs)

    async def flow(ctx, _args):
        output = await ctx.agent("Write one note.", tools=list(builtin_tools("file_write")))
        for session in ctx.sessions:
            await session.aclose()
        await asyncio.gather(
            ctx.release_isolated_workspaces(),
            ctx.release_isolated_workspaces(),
        )
        return output

    client = OpenCollab(tmp_path, environment_providers={"custom": provider})
    result = await client.workflow(flow, isolation="custom", llm=Writer(), trace=False)
    assert result.ok and result.output == "finished"
    checkout, = removed
    assert not checkout.exists()
    assert (tmp_path / "seed.txt").read_text() == "initial"


async def test_concurrent_session_pool_shutdown_releases_each_backend_once(tmp_path):
    _repo(tmp_path, dirty=False)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    class ObservedPool(WorktreePool):
        async def release(self):
            calls.append("release")
            entered.set()
            await finish.wait()
            await super().release()

    def provider(**kwargs):
        return ObservedPool(use_worktrees=True, **kwargs)

    pool = SessionEnvironmentPool(str(tmp_path), isolation="custom", providers={"custom": provider})
    workspace = await pool.acquire("worker")
    first = asyncio.create_task(pool.release())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(pool.release())
        await asyncio.sleep(0)
    finally:
        finish.set()
    await asyncio.gather(first, second)
    assert calls == ["release"]
    assert not Path(workspace.workspace).exists()


@pytest.mark.parametrize("shutdown", [False, True])
async def test_concurrent_release_waits_for_the_same_workspace_owner(tmp_path, shutdown):
    _repo(tmp_path, dirty=False)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    class ObservedPool(WorktreePool):
        async def release_env(self, env):
            calls.append(env)
            entered.set()
            await finish.wait()
            await super().release_env(env)

    def provider(**kwargs):
        return ObservedPool(use_worktrees=True, **kwargs)

    pool = SessionEnvironmentPool(str(tmp_path), isolation="custom", providers={"custom": provider})
    workspace = await pool.acquire("worker")
    first = asyncio.create_task(pool.release_env(workspace))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(pool.release() if shutdown else pool.release_env(workspace))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        finish.set()
    await asyncio.gather(first, second)
    await pool.release()
    assert calls == [workspace] and not Path(workspace.workspace).exists()


@pytest.mark.parametrize("entry_isolation", ["shared", "worktree"])
async def test_team_role_override_and_isolated_entry_patch(tmp_path, monkeypatch, entry_isolation):
    _repo(tmp_path)
    team = tmp_path / "team.yaml"
    team.write_text("""entry: manager
isolation: worktree
roles:
  manager:
    prompt: Manager
    tools: [spawn_agent]
  coder:
    prompt: Coder
    isolation: shared
    tools: [file_write]
topology:
  manager: [coder]
""")

    class TeamLLM(Writer):
        async def complete(self, messages, **kwargs):
            if messages[0]["content"].startswith("Manager"):
                if any(m["role"] == "tool" for m in messages):
                    return LLMResponse(content="collected", usage=Usage(3, 2), finish_reason="stop")
                return LLMResponse(tool_calls=[_call("spawn_agent", {"role": "coder", "task": "edit"})],
                                   usage=Usage(3, 2), finish_reason="tool_calls")
            if any(m["role"] == "tool" for m in messages):
                return LLMResponse(content="edited", usage=Usage(3, 2), finish_reason="stop")
            return LLMResponse(tool_calls=[_call("file_write", {
                "mode": "create", "path": "child.txt", "content": "delivered",
            })], usage=Usage(3, 2), finish_reason="tool_calls")

    monkeypatch.setattr(container, "LLMClient", TeamLLM)
    result = await OpenCollab(tmp_path).team("delegate", config=team, isolation=entry_isolation,
                                           artifacts=tmp_path / "run", trace=False)
    assert result.ok and result.output == "collected"
    assert (tmp_path / "child.txt").exists() is (entry_isolation == "shared")
    if entry_isolation == "worktree":
        patch = Path(result.metrics["entry_patch"])
        assert patch.exists() and "+delivered" in patch.read_text()


async def test_unknown_strategy_fails_before_model_use(tmp_path):
    async def flow(ctx, args):
        return await ctx.agent("marker", tools=[])

    class NoModel(Writer):
        async def complete(self, *args, **kwargs):
            raise AssertionError("model must not be called")

    result = await OpenCollab(tmp_path).workflow(flow, isolation="missing", llm=NoModel(), trace=False)
    assert result.agent_failures
    assert result.agent_failures[0]["exception_type"] == "ValueError"
