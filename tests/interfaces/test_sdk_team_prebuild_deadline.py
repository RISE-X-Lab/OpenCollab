from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.bootstrap import container, programmatic
from tests.interfaces.test_session_isolation import _repo


@pytest.mark.parametrize("stop", [
    "none", "deadline", "caller", "retry", "deadline-settles", "caller-settles",
    "caller-unbounded", "caller-unbounded-settles",
])
async def test_team_deadline_during_owned_prebuild_releases_after_startup_stops(tmp_path, monkeypatch, stop):
    workspace = tmp_path / "source"
    workspace.mkdir()
    _repo(workspace, dirty=False)
    entered, continue_setup, setup_done, pool_released = (asyncio.Event() for _ in range(4))
    objects = {}
    requests = []
    caller_stops = stop.startswith("caller")
    deadline_stops = stop in {"deadline", "retry", "deadline-settles"}
    finishes_on_cancel = stop.endswith("-settles")
    deadline = 1.5 if deadline_stops else None if stop.startswith("caller-unbounded") else 8

    class LocalModel:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 100_000

        async def complete(self, messages, **kwargs):
            requests.append(messages)
            return LLMResponse(content="Inventory approved.", usage=Usage(3, 2), finish_reason="stop")

        async def close(self):
            pass

    class DelayedWorktreePool(WorktreePool):
        async def acquire(self, role, *, source=None):
            async def configure_checkout():
                env = await super(DelayedWorktreePool, self).acquire(role, source=source)
                objects["worker"] = env
                entered.set()
                await continue_setup.wait()
                objects["read"] = await env.read_file("seed.txt")
                setup_done.set()
                return env

            if not finishes_on_cancel:
                return await await_owned_operation(configure_checkout(), propagate_cancellation=True)
            owner = asyncio.create_task(configure_checkout())
            while True:
                try:
                    return await asyncio.shield(owner)
                except asyncio.CancelledError:
                    continue_setup.set()

        async def release(self):
            await super().release()
            pool_released.set()

    def provider(**options):
        return DelayedWorktreePool(use_worktrees=True, **options)

    original = programmatic.build_scheduler

    def build(*args, **kwargs):
        scheduler = original(*args, **kwargs)
        objects["scheduler"] = scheduler
        return scheduler

    monkeypatch.setattr(container, "LLMClient", LocalModel)
    monkeypatch.setattr(programmatic, "build_scheduler", build)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    config = tmp_path / "team.yaml"
    config.write_text("""entry: lead
roles:
  lead:
    prompt: Report the inventory.
    tools: [message_agent]
  worker:
    prompt: Verify the inventory.
    tools: []
    isolation: reports
topology:
  lead: [worker]
  worker: []
""")
    source = LocalEnvironment(str(workspace))
    app = OpenCollab(workspace, environment=source, environment_providers={"reports": provider})
    cancellation = []

    async def run():
        try:
            return await app.team(
                "Report the inventory.", config=config, budget=50_000, prebuild_team=True, use_worktrees=False,
                timeout=deadline, cleanup_timeout=.1,
                artifacts=tmp_path / "artifacts", trace=False,
            )
        except asyncio.CancelledError as exc:
            cancellation.append(exc.args)
            raise

    if stop == "none":
        continue_setup.set()
    operation = asyncio.create_task(run())
    try:
        try:
            await asyncio.wait_for(entered.wait(), 5)
        except asyncio.TimeoutError:
            if operation.done():
                pytest.fail(f"Team stopped before worker setup: {operation.result()!r}")
            raise
        checkout = Path(objects["worker"].workspace)
        if caller_stops:
            operation.cancel("caller interrupted team preparation")
        done, _ = await asyncio.wait({operation}, timeout=3)
        print(f"stop={stop!r} returned_while_held={operation in done} checkout_present={checkout.exists()} "
              f"requests={len(requests)}")
        assert operation in done
        if caller_stops:
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert cancellation == [("caller interrupted team preparation",)]
        else:
            result = await operation
            if deadline_stops:
                assert (result.status, result.reason, result.tokens, result.output) == ("stopped", "timeout", 0, None)
                if not finishes_on_cancel:
                    assert result.metrics["execution_quiesced"] is False
            else:
                assert (result.status, result.output, result.tokens) == ("completed", "Inventory approved.", 5)
        if stop != "none" and not finishes_on_cancel:
            assert not setup_done.is_set() and not pool_released.is_set()
            assert checkout.is_dir() and not requests
        if stop == "retry":
            with pytest.raises(RuntimeError, match="execution tasks did not quiesce"):
                await asyncio.wait_for(objects["scheduler"].cleanup(cleanup_timeout=.1), 2)
            assert checkout.is_dir() and not pool_released.is_set()
        continue_setup.set()
        await asyncio.wait_for(setup_done.wait(), 5)
        assert objects["read"] == "initial"
        await asyncio.wait_for(pool_released.wait(), 5)
        assert not checkout.exists()
        assert await source.read_file("seed.txt") == "initial"
        assert len(requests) == (1 if stop == "none" else 0)
    finally:
        continue_setup.set()
        await asyncio.gather(operation, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(setup_done.wait(), 5)
        if "scheduler" in objects:
            await objects["scheduler"].cleanup()
        await source.cleanup()
