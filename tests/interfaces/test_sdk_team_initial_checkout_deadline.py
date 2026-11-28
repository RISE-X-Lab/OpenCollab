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


@pytest.mark.parametrize("mode", ["success", "deadline", "caller", "caller-settles", "shared-budget", "failure"])
async def test_team_initial_checkout_respects_deadline_and_cancellation(tmp_path, monkeypatch, mode):
    workspace = tmp_path / "source"
    workspace.mkdir()
    _repo(workspace, dirty=False)
    entered, finish_setup, setup_done, released = (asyncio.Event() for _ in range(4))
    built = []
    requests = []
    objects = {}
    cancellations = []
    preparation_failure = OSError("checkout preparation failed")

    class Model:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 100_000

        async def complete(self, messages, **kwargs):
            requests.append(messages)
            if mode == "shared-budget":
                await asyncio.Event().wait()
            return LLMResponse(content="Inventory approved.", usage=Usage(3, 2), finish_reason="stop")

        async def close(self):
            pass

    class DelayedCheckout(WorktreePool):
        async def acquire(self, role, *, source=None):
            async def configure():
                env = await super(DelayedCheckout, self).acquire(role, source=source)
                objects["checkout"] = Path(env.workspace)
                entered.set()
                await finish_setup.wait()
                objects["seed"] = await env.read_file("seed.txt")
                setup_done.set()
                if mode == "failure":
                    raise preparation_failure
                return env

            if mode != "caller-settles":
                return await await_owned_operation(configure(), propagate_cancellation=True)
            owner = asyncio.create_task(configure())
            while True:
                try:
                    return await asyncio.shield(owner)
                except asyncio.CancelledError:
                    finish_setup.set()

        async def release(self):
            await super().release()
            released.set()
            if mode == "failure":
                raise OSError("checkout release receipt failed")

    def provider(**options):
        pool = DelayedCheckout(use_worktrees=True, **options)
        objects["pool"] = pool
        return pool

    original = programmatic.build_scheduler

    def build(*args, **kwargs):
        scheduler = original(*args, **kwargs)
        built.append(scheduler)
        return scheduler

    monkeypatch.setattr(container, "LLMClient", Model)
    monkeypatch.setattr(programmatic, "build_scheduler", build)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    config = tmp_path / "team.yaml"
    config.write_text("entry: lead\nroles:\n  lead:\n    prompt: Verify inventory.\n    tools: []\n")
    artifacts = tmp_path / "artifacts"
    source = LocalEnvironment(str(workspace))
    app = OpenCollab(workspace, environment=source, environment_providers={"reports": provider})
    timeout = {"deadline": 1.2, "shared-budget": 3.4}.get(mode)

    async def run():
        try:
            return await app.team(
                "Verify the inventory.", config=config, budget=50_000, use_worktrees=False,
                isolation="reports", timeout=timeout,
                cleanup_timeout=.1, artifacts=artifacts, trace=True,
            )
        except asyncio.CancelledError as exc:
            cancellations.append(exc.args)
            raise

    if mode in {"success", "failure"}:
        finish_setup.set()
    running = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if mode.startswith("caller"):
            running.cancel("caller interrupted Lead checkout")
        if mode == "shared-budget":
            await asyncio.sleep(1.8)
            finish_setup.set()
        done, _ = await asyncio.wait({running}, timeout=2.45 if mode == "shared-budget" else 2.8)
        before_finish = bool(done)
        print(f"mode={mode!r} returned_before_release={before_finish} models_at_return={len(requests)}")
        if mode == "caller" or mode == "deadline":
            assert objects["checkout"].is_dir() and not setup_done.is_set() and not released.is_set()
        finish_setup.set()
        result, = await asyncio.gather(running, return_exceptions=True)
        print(f"outcome={getattr(result, 'status', type(result).__name__)!r} model_requests={len(requests)}")
        assert before_finish
        if mode.startswith("caller"):
            assert isinstance(result, asyncio.CancelledError)
            assert cancellations == [("caller interrupted Lead checkout",)]
        elif mode == "failure":
            assert result is preparation_failure
            assert any("checkout release receipt failed" in note for note in result.__notes__)
        elif mode == "deadline":
            assert (result.status, result.reason, result.tokens, result.output) == ("stopped", "timeout", 0, None)
            assert result.metrics["sessions"] == 0 and result.metrics["execution_quiesced"] is False
        elif mode == "shared-budget":
            assert (result.status, result.reason, result.tokens, result.output) == ("stopped", "timeout", 0, None)
            assert result.metrics["sessions"] == 1
        else:
            assert (result.status, result.tokens, result.output) == ("completed", 5, "Inventory approved.")

        await asyncio.wait_for(setup_done.wait(), 5)
        await asyncio.wait_for(released.wait(), 5)
        assert objects["seed"] == "initial"
        assert not objects["checkout"].exists()
        assert await source.read_file("seed.txt") == "initial"
        initialized = mode in {"success", "shared-budget"}
        assert len(requests) == len(built) == int(initialized)
        assert (artifacts / "team.json").exists() is initialized
    finally:
        finish_setup.set()
        await asyncio.gather(running, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(setup_done.wait(), 5)
        if built:
            await built[0].cleanup()
        elif "pool" in objects and not released.is_set():
            await objects["pool"].release()
        await source.cleanup()
