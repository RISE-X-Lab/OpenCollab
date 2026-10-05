"""Preserve captured candidate edits through source cancellation and session close."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap import _workflow_runtime_session as runtime
from tests.support.asyncio_test_support import assert_cancel_note
from tests.support.session_run_loop_test_support import llm_response
from tests.workflows.test_candidate_source_verification import _repository


@pytest.mark.parametrize("cancel_verification", [False, True])
@pytest.mark.parametrize("close_error", [False, True])
async def test_candidate_source_cancellation_survives_isolated_session_close(
    tmp_path, monkeypatch, cancel_verification, close_error,
):
    repo = _repository(tmp_path)
    base = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(base)
    leases, captured_patches, closes, model_reads = [], [], [], []
    acquire, source_diff = workspace.acquire, workspace.source_diff
    verification_started = asyncio.Event()
    reads = 0

    async def recorded_acquire(label):
        lease = await acquire(label)
        leases.append(lease)

        class CapturingLease:
            def __getattr__(self, name):
                return getattr(lease, name)

            async def diff(self, *args, **kwargs):
                patch = await lease.diff(*args, **kwargs)
                captured_patches.append(patch)
                return patch

        return CapturingLease()

    async def post_capture_source_read(exclude_paths=()):
        nonlocal reads
        reads += 1
        if reads == 2:
            verification_started.set()
            if cancel_verification:
                await asyncio.Event().wait()
        return await source_diff(exclude_paths)

    class ModelTransport:
        def __init__(self, environment):
            self.environment = environment

        async def complete(self, *args, **kwargs):
            value = await self.environment.read_file("source.py")
            model_reads.append(value)
            return llm_response(content=value, total_tokens=3)

        async def close(self):
            closes.append(self.environment.workspace)
            if close_error:
                raise OSError("model transport close failed")

    original_build = runtime.build_session

    def build(**kwargs):
        session = original_build(**kwargs, llm=ModelTransport(kwargs["env"]))
        session._owns_llm = True
        return session

    monkeypatch.setattr(runtime, "build_session", build)
    monkeypatch.setattr(workspace, "acquire", recorded_acquire)
    monkeypatch.setattr(workspace, "source_diff", post_capture_source_read)
    factory = runtime.WorkflowSessionFactory(
        model="test-model", provider="openai", api_key=None, base_url=None,
        workspace=str(repo), env=base,
    )
    parent = WorkflowContext(factory, budget_total=100_000, candidate_workspace=workspace)

    async def nested(child, _args):
        await child._factory._environment.write_file("source.py", "value = 2\n")
        return await child.agent("read edited candidate", isolation=True)

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(verification_started.wait(), timeout=5)
        if cancel_verification:
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as failure:
                await task
            if close_error:
                assert_cancel_note(failure.value, "model transport close failed")
        else:
            candidate = await task
            assert candidate.output == "value = 2\n"
            assert "+value = 2" in candidate.diff
            assert bool(candidate.lifecycle_errors) is close_error

        assert captured_patches and "+value = 2" in captured_patches[0]
        assert model_reads == ["value = 2\n"]
        assert (repo / "source.py").read_text() == "value = 1\n"
        assert len(closes) == 1
        assert not factory._candidate_isolation_leases
        assert not parent.budget._leases
        candidate_path = Path(leases[0].candidate_workspace)
        assert candidate_path.is_dir() is cancel_verification
        if cancel_verification:
            assert (candidate_path / "source.py").read_text() == "value = 2\n"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for lease in leases:
            await lease.cleanup()
        await factory.release_isolated_envs()
        await base.cleanup()
