from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.bootstrap import container, programmatic
from tests.interfaces.test_session_isolation import _repo


@pytest.mark.parametrize("source_owner", ["sdk", "caller"])
@pytest.mark.parametrize("close_mode", ["immediate", "late", "retry", "attached-runtime"])
async def test_team_returns_worktree_after_provider_close_settles(tmp_path, monkeypatch, source_owner, close_mode):
    workspace = tmp_path / "source"
    workspace.mkdir()
    _repo(workspace, dirty=False)
    entered, finish, closed, removed = (asyncio.Event() for _ in range(4))
    objects = {}

    class LocalModel:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 100_000

        async def complete(self, messages, **kwargs):
            return LLMResponse(content="Inventory sent.", usage=Usage(3, 2), finish_reason="stop")

        async def close(self):
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    pass
            try:
                objects["report"] = await objects["entry"].read_file("seed.txt")
            finally:
                closed.set()

    class RecordingPool(WorktreePool):
        async def acquire(self, role, *, source=None):
            environment = await super().acquire(role, source=source)
            objects["entry"] = environment
            return environment

        async def release_env(self, environment):
            await super().release_env(environment)
            removed.set()

    def provider(**options):
        pool = RecordingPool(use_worktrees=True, **options)
        objects["pool"] = pool
        return pool

    build_scheduler = programmatic.build_scheduler

    def remember_scheduler(*args, **kwargs):
        scheduler = build_scheduler(*args, **kwargs)
        objects["scheduler"] = scheduler
        if close_mode == "attached-runtime":
            run = scheduler.run
            objects["runtime_reads"] = []

            async def close_report():
                entered.set()
                await finish.wait()
                objects["runtime_reads"].append(await objects["entry"].read_file("seed.txt"))

            async def run_with_report(prompt):
                output = await run(prompt)
                scheduler.lead_session.own_runtime(close_report)
                return output

            monkeypatch.setattr(scheduler, "run", run_with_report)
        return scheduler

    monkeypatch.setattr(programmatic, "build_scheduler", remember_scheduler)
    monkeypatch.setattr(container, "LLMClient", LocalModel)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    team = tmp_path / "team.yaml"
    team.write_text("entry: lead\nroles:\n  lead:\n    tools: []\n    prompt: Report the inventory.\n")
    caller_env = LocalEnvironment(str(workspace)) if source_owner == "caller" else None
    if close_mode == "immediate":
        finish.set()
    app = OpenCollab(workspace, environment=caller_env, environment_providers={"reports": provider})
    operation = asyncio.create_task(app.team("Report the inventory.", config=team,
                                            isolation="reports", trace=False, cleanup_timeout=.1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        done, _ = await asyncio.wait({operation}, timeout=2)
        assert operation in done
        result = await operation
        checkout = Path(objects["entry"].workspace)
        print(f"source={source_owner} mode={close_mode} status={result.status!r} output={result.output!r} "
              f"tokens={result.tokens} checkout_at_return={checkout.exists()}")
        assert result.output == "Inventory sent." and result.tokens == 5
        assert result.status == ("completed" if close_mode == "immediate" else "failed")
        if close_mode != "immediate":
            assert checkout.is_dir() and not closed.is_set() and not removed.is_set()
            if close_mode == "retry":
                try:
                    with pytest.raises(RuntimeError, match="session resource close failed or timed out"):
                        await asyncio.wait_for(objects["scheduler"].cleanup(cleanup_timeout=.1), 2)
                finally:
                    print(f"checkout during retry={checkout.is_dir()} released={removed.is_set()}")
                assert checkout.is_dir() and not removed.is_set()
            finish.set()
        await asyncio.wait_for(closed.wait(), 5)
        assert objects["report"] == "initial"
        try:
            await asyncio.wait_for(removed.wait(), 3)
        except asyncio.TimeoutError:
            pytest.fail("Team retained the Git worktree after its provider close settled")
        assert not checkout.exists()
        if close_mode == "attached-runtime":
            await objects["scheduler"].cleanup()
            assert objects["runtime_reads"] == ["initial"]
        if caller_env is not None:
            assert await caller_env.read_file("seed.txt") == "initial"
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(closed.wait(), 5)
        if "scheduler" in objects:
            await objects["scheduler"].cleanup()
        if caller_env is not None:
            await caller_env.cleanup()
