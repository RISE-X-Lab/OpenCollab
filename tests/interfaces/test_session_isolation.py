from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path

import pytest

from opencollab import OpenCollab, RunError
from opencollab.adapters.env import LocalEnvironment
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


async def test_pool_keeps_failed_child_source_and_releases_independent_worktree(tmp_path, monkeypatch):
    _repo(tmp_path, dirty=False)

    def provider(**options):
        return WorktreePool(use_worktrees=True, **options)

    pool = SessionEnvironmentPool(str(tmp_path), providers={"report": provider})
    ready = False
    try:
        parent = await pool.acquire("parent")
        independent = await pool.acquire("independent")
        child = await pool.acquire("child", isolation="report", source=parent)
        cleanup = child.cleanup

        async def delayed_cleanup():
            if not ready:
                raise OSError("report not copied")
            await cleanup()

        monkeypatch.setattr(child, "cleanup", delayed_cleanup)
        checkout = Path(parent.workspace)
        with pytest.raises(OSError, match="report not copied"):
            await pool.release()
        assert await parent.read_file("seed.txt") == "initial"
        assert checkout.is_dir() and Path(child.workspace).is_dir()
        assert not Path(independent.workspace).exists()
    finally:
        ready = True
        await pool.release()
    assert not checkout.exists()


@pytest.mark.parametrize("late_source", [False, True])
@pytest.mark.parametrize("export_fault", [False, True])
async def test_pool_retains_sources_of_a_reused_environment(tmp_path, late_source, export_fault):
    _repo(tmp_path, dirty=False)
    report = tmp_path / "report.txt"
    release_attempts = 0
    export_enabled = True

    class ReportPool(WorktreePool):
        def __init__(self, **options):
            super().__init__(use_worktrees=True, **options)
            self.environment = None
            self.sources = []

        async def acquire(self, role, *, source=None):
            self.sources.append(source)
            if self.environment is None:
                self.environment = await super().acquire(role, source=source)
            return self.environment

        async def release_env(self, env):
            nonlocal release_attempts
            release_attempts += 1
            if export_enabled:
                if export_fault and release_attempts == 1:
                    raise OSError("temporary report destination error")
                details = [await source.read_file("detail.txt") for source in self.sources]
                report.write_text(" / ".join(details))
            await super().release_env(env)

    pool = SessionEnvironmentPool(str(tmp_path), providers={"report": ReportPool})
    try:
        first = await pool.acquire("first")
        if not late_source:
            second = await pool.acquire("second")
        view = await pool.acquire("combine-first", source=first, isolation="report")
        if late_source:
            second = await pool.acquire("second")
        again = await pool.acquire("combine-second", source=second, isolation="report")
        independent = await pool.acquire("independent")
        assert view is again
        await first.write_file("detail.txt", "first")
        await second.write_file("detail.txt", "second")
        await pool.release_env(again)
        assert release_attempts == 0

        if export_fault:
            with pytest.raises(OSError, match="temporary report destination error"):
                await pool.release()
            assert not Path(independent.workspace).exists()
            assert await first.read_file("detail.txt") == "first"
            assert await second.read_file("detail.txt") == "second"
            assert Path(view.workspace).is_dir()

        await pool.release()
        assert report.read_text() == "first / second"
        assert release_attempts == (2 if export_fault else 1)
        assert all(not Path(env.workspace).exists() for env in (first, second, view, independent))
    finally:
        export_enabled = False
        await pool.release()


@pytest.mark.parametrize("phase", ["session-close", "workspace-release"])
@pytest.mark.parametrize("mode", ["cleanup-timeout", "caller-cancelled", "workflow-timeout"])
async def test_workflow_preserves_workspace_until_owned_cleanup_settles(tmp_path, monkeypatch, phase, mode):
    workspace = tmp_path / "source"
    workspace.mkdir()
    _repo(workspace, dirty=False)
    entered, finish, flushed, source_closed = (asyncio.Event() for _ in range(4))
    observed = {}
    loop = asyncio.get_running_loop()
    started = loop.time()
    workflow_timeout = 4 if mode == "workflow-timeout" else None

    class ReportPool(WorktreePool):
        async def acquire(self, role, *, source=None):
            environment = await super().acquire(role, source=source)
            observed["environment"] = environment
            if phase == "workspace-release":
                cleanup = environment.cleanup

                async def release_report():
                    entered.set()
                    await finish.wait()
                    observed["report"] = await observed["source"].read_file("seed.txt")
                    await cleanup()
                    flushed.set()

                monkeypatch.setattr(environment, "cleanup", release_report)
            return environment

    def provider(**options):
        source = options["base_environment"]
        observed["source"] = source
        cleanup = source.cleanup

        async def close_source():
            await cleanup()
            source_closed.set()

        monkeypatch.setattr(source, "cleanup", close_source)
        pool = ReportPool(use_worktrees=True, **options)
        observed["pool"] = pool
        return pool

    async def flow(ctx, _args):
        output = await ctx.agent("Write one report.", tools=list(builtin_tools("file_write")))
        if phase == "session-close":
            session, = ctx.sessions

            async def copy_report():
                entered.set()
                await finish.wait()
                try:
                    observed["report"] = await observed["environment"].read_file("marker.txt")
                finally:
                    flushed.set()

            session.own_runtime(copy_report)
        if workflow_timeout is not None:
            await asyncio.sleep(max(0, started + workflow_timeout - .25 - loop.time()))
        return output

    client = OpenCollab(workspace, environment_providers={"report": provider})

    async def run():
        try:
            return await client.workflow(
                flow, llm=Writer(), trace=False, isolation="report", timeout=workflow_timeout,
                cleanup_timeout=.6 if workflow_timeout is not None else .1,
            )
        except asyncio.CancelledError as exc:
            observed["cancellation"] = exc.args
            raise

    operation = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 8)
        if mode == "caller-cancelled":
            operation.cancel("caller stopped")
        done, _ = await asyncio.wait({operation}, timeout=2)
        assert operation in done
        expected_error = asyncio.CancelledError if mode == "caller-cancelled" else RunError
        with pytest.raises(expected_error):
            await operation
        assert Path(observed["environment"].workspace).is_dir()
        assert not source_closed.is_set()
        assert await observed["source"].read_file("seed.txt") == "initial"
        if mode == "caller-cancelled":
            assert observed["cancellation"] == ("caller stopped",)
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)
        try:
            if entered.is_set():
                await asyncio.wait_for(flushed.wait(), 5)
                await asyncio.wait_for(source_closed.wait(), 5)
                assert not Path(observed["environment"].workspace).exists()
        finally:
            if "pool" in observed:
                await observed["pool"].release()
    assert observed["report"] == ("marker" if phase == "session-close" else "initial")


@pytest.mark.parametrize("caller_owns_source", [False, True])
async def test_workflow_removes_worktree_after_final_save_settles(tmp_path, monkeypatch, caller_owns_source):
    repository = tmp_path / "source"
    repository.mkdir()
    _repo(repository, dirty=False)
    saving, removed, source_closed = (asyncio.Event() for _ in range(3))
    finish_save = threading.Event()
    loop = asyncio.get_running_loop()
    observed = {}

    class ReportPool(WorktreePool):
        async def acquire(self, role, *, source=None):
            workspace = await super().acquire(role, source=source)
            observed["workspace"] = workspace
            return workspace

        async def release_env(self, env):
            await super().release_env(env)
            removed.set()

    def provider(**options):
        source = options["base_environment"]
        cleanup = source.cleanup

        async def close_source():
            await cleanup()
            source_closed.set()

        monkeypatch.setattr(source, "cleanup", close_source)
        pool = ReportPool(use_worktrees=True, **options)
        observed["pool"] = pool
        return pool

    async def flow(ctx, _args):
        answer = await ctx.agent("Write a report.", tools=list(builtin_tools("file_write")))
        await ctx.wait_for_pending_cleanup()
        session, = ctx.sessions
        append = session.store.append_snapshot_delta

        def finish_delayed_save(path, **kwargs):
            loop.call_soon_threadsafe(saving.set)
            if not finish_save.wait(10):
                raise TimeoutError("Test did not release the final save")
            append(path, **kwargs)
            observed["saved"] = True

        monkeypatch.setattr(session.store, "append_snapshot_delta", finish_delayed_save)
        return answer

    source = LocalEnvironment(str(repository)) if caller_owns_source else None
    client = OpenCollab(repository, environment=source, environment_providers={"report": provider})
    operation = asyncio.create_task(client.workflow(
        flow, llm=Writer(), trace=False, isolation="report", cleanup_timeout=.1, artifacts=tmp_path / "artifacts",
    ))
    try:
        await asyncio.wait_for(saving.wait(), 8)
        done, _ = await asyncio.wait({operation}, timeout=2)
        assert operation in done
        with pytest.raises(RunError):
            await operation
        checkout = Path(observed["workspace"].workspace)
        assert (checkout / "marker.txt").read_text() == "marker"
        assert not removed.is_set() and not source_closed.is_set()

        finish_save.set()
        await asyncio.wait_for(removed.wait(), 5)
        assert observed["saved"] and not checkout.exists()
        if source is None:
            await asyncio.wait_for(source_closed.wait(), 5)
        else:
            assert not source_closed.is_set()
            assert await source.read_file("seed.txt") == "initial"
    finally:
        finish_save.set()
        await asyncio.gather(operation, return_exceptions=True)
        if "pool" in observed:
            await observed["pool"].release()
        if source is not None:
            await source.cleanup()


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
