"""Public SDK workflow cancellation preserves finalized artifacts."""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.bootstrap import (
    _workflow_runtime_execution as workflow_execution,
)
from opencollab.bootstrap import programmatic
from tests.interfaces.test_sdk_runtime import ReplyLLM


@pytest.mark.parametrize(("trace", "failure"), [
    (False, None), (True, None), (True, "trace"), (True, "claim"),
])
async def test_caller_cancellation_finalizes_proven_workflow_evidence(tmp_path, monkeypatch, trace, failure):
    artifacts = tmp_path / "cancelled-workflow"
    ready = asyncio.Event()
    finished = asyncio.Event()
    if failure == "trace":
        monkeypatch.setattr(workflow_execution, "_inspect_tracer", lambda _tracer: ("trace write failed", 1, None))

    async def flow(ctx, _inputs):
        assert await ctx.agent("finish once", tools=[], label="worker") == "finished"
        if failure == "claim":
            (artifacts / ".opencollab-run").unlink()
        ready.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    running = asyncio.create_task(OpenCollab(tmp_path, model="model").workflow(
        flow, run_id="cancelled-run", artifacts=artifacts, trace=trace, llm=ReplyLLM(),
    ))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        running.cancel("caller cancelled")
        done, _ = await asyncio.wait({running}, timeout=5)
        assert running in done
        with pytest.raises(asyncio.CancelledError, match="caller cancelled") as caught:
            await running
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert finished.is_set()
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["run_id"] == "cancelled-run"
    assert manifest["status"] == "stopped"
    assert manifest["reason"] == "cancelled"
    assert manifest["sessions"] == 1
    assert manifest["tokens_spent"] == 6
    assert manifest["evidence_complete"] is (failure is None)
    assert (artifacts / "orchestration.jsonl").exists() is trace
    if failure is not None:
        expected_note = "trajectory write failed" if failure == "trace" else "run artifact claim"
        assert any(expected_note in note for note in caught.value.__notes__)

@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_caller_cancellation_waits_for_owned_workflow_manifest_finalization(
    tmp_path, monkeypatch, cleanup_fails,
):
    artifacts = tmp_path / "cleanup-cancelled-workflow"
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    original_cleanup = programmatic.LocalEnvironment.cleanup

    async def cleanup(environment):
        cleanup_started.set()
        await release_cleanup.wait()
        await original_cleanup(environment)
        if cleanup_fails:
            raise OSError("cleanup failed")

    monkeypatch.setattr(programmatic.LocalEnvironment, "cleanup", cleanup)

    async def flow(ctx, _inputs):
        return await ctx.agent("finish once", tools=[])

    running = asyncio.create_task(OpenCollab(tmp_path, model="model").workflow(
        flow, run_id="completed-run", artifacts=artifacts, trace=False, llm=ReplyLLM(), cleanup_timeout=5,
    ))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 5)
        manifest_path = artifacts / "workflow.json"
        before = json.loads(manifest_path.read_text())
        assert before["status"] == "completed"
        assert before["evidence_complete"] is False
        running.cancel("caller cancelled")
        await asyncio.sleep(0)
        running.cancel("caller cancelled again")
        await asyncio.sleep(0)
        assert not running.done()
        release_cleanup.set()
        done, _ = await asyncio.wait({running}, timeout=5)
        assert running in done
        with pytest.raises(asyncio.CancelledError) as caught:
            await running
    finally:
        release_cleanup.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert caught.value.args == ("caller cancelled",)
    after = json.loads(manifest_path.read_text())
    assert after["run_id"] == "completed-run"
    assert after["tokens_spent"] == 6
    assert after["evidence_complete"] is (not cleanup_fails)
    if cleanup_fails:
        assert after["status"] == "failed"
        assert after["reason"] == "environment_cleanup_failed"
        assert any("workflow-owned environment cleanup failed" in note for note in caught.value.__notes__)
    else:
        assert after["status"] == "completed"
        assert after["reason"] is None
