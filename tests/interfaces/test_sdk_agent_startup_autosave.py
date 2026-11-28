from __future__ import annotations

import asyncio
import fcntl
import threading

import pytest

from opencollab import OpenCollab, RunError
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import container, programmatic


@pytest.mark.parametrize("stop", ["none", "deadline", "caller", "late"])
async def test_agent_startup_waits_for_the_autosave_journal_off_the_event_loop(tmp_path, monkeypatch, stop):
    artifacts = tmp_path / "artifacts"
    transcript = artifacts / "agent.json"
    locked = asyncio.Event()
    release_requested, released = threading.Event(), threading.Event()
    lock_threads = []
    requests = []
    model_closed = asyncio.Event()
    cancellations = []

    class ContendedLocalEnvironment(LocalEnvironment):
        def __init__(self, workspace):
            super().__init__(workspace)
            reader = (artifacts / "agent.json.journal.lock").open("a+")
            fcntl.flock(reader, fcntl.LOCK_EX)

            def finish_read():
                release_requested.wait(3)
                reader.close()
                released.set()

            thread = threading.Thread(target=finish_read)
            lock_threads.append(thread)
            thread.start()
            locked.set()

    class InventoryModel:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            model_closed.set()

        def context_window(self):
            return 100_000

        async def complete(self, messages, **kwargs):
            requests.append(messages)
            return LLMResponse(content="Inventory verified.", usage=Usage(3, 2), finish_reason="stop")

    monkeypatch.setattr(programmatic, "LocalEnvironment", ContendedLocalEnvironment)
    monkeypatch.setattr(container, "LLMClient", InventoryModel)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    app = OpenCollab(tmp_path, model="gpt-4o")
    async def run():
        try:
            return await app.agent(
                "Check the inventory.", llm=None if stop == "late" else InventoryModel(), tools=[],
                artifacts=artifacts, trace=False, timeout=.15 if stop in {"deadline", "late"} else 5,
                cleanup_timeout=.05 if stop == "late" else 1,
            )
        except asyncio.CancelledError as exc:
            cancellations.append(exc.args)
            raise

    operation = asyncio.create_task(run())
    try:
        await asyncio.wait_for(locked.wait(), 2)
        await asyncio.sleep(.03)
        assert not released.is_set()
        assert not requests
        if stop == "caller":
            operation.cancel("stop before the first request")
        if stop == "late":
            with pytest.raises(RunError, match="timed-out agent did not reach a quiescent terminal state"):
                await asyncio.wait_for(asyncio.shield(operation), 2)
            assert not released.is_set()
            assert not requests
            assert SessionStore().try_autosave_lock(str(transcript)) is None
            release_requested.set()

            async def reacquire():
                while True:
                    unlock = SessionStore().try_autosave_lock(str(transcript))
                    if unlock is not None:
                        unlock()
                        return
                    await asyncio.sleep(.01)

            await asyncio.wait_for(reacquire(), 1)
            await asyncio.wait_for(model_closed.wait(), 1)
            return
        if stop == "deadline":
            await asyncio.sleep(.20)
        release_requested.set()
        if stop == "caller":
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert cancellations == [("stop before the first request",)]
        else:
            result = await operation
            if stop == "none":
                assert (result.status, result.output, result.tokens) == ("completed", "Inventory verified.", 5)
                assert len(requests) == 1
            else:
                assert (result.status, result.reason, result.output, result.tokens) == ("stopped", "timeout", None, 0)
        snapshot = app.read_session_snapshot(transcript)
        if stop == "none":
            assert snapshot["messages"][-1]["content"] == "Inventory verified."
        else:
            assert not requests
            assert all(row["role"] == "system" for row in snapshot["messages"])
        unlock = SessionStore().try_autosave_lock(str(transcript))
        assert unlock is not None
        unlock()
    finally:
        release_requested.set()
        await asyncio.gather(operation, return_exceptions=True)
        for thread in lock_threads:
            thread.join()
