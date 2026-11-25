from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


@pytest.mark.parametrize("cancel", [False, True])
async def test_candidate_releases_outer_worktree_when_cancelled_during_isolated_cleanup(
    tmp_path, monkeypatch, cancel,
):
    repository = _repository(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    candidates, isolated = [], []

    class Model:
        async def complete(self, messages, **kwargs):
            return LLMResponse(content="checked", usage=Usage(5, 10))

    class CandidateWorkspace(EnvCandidateWorkspace):
        async def acquire(self, label):
            lease = await super().acquire(label)
            candidates.append(lease)
            return lease

    async def edit(child, _inputs):
        (Path(child.workspace_root) / "report.txt").write_text("prepared report\n")
        assert await child.agent("review report", isolation="worktree", tools=[]) == "checked"
        environment = child.sessions[-1].env
        isolated.append(Path(environment.workspace))
        cleanup = environment.cleanup

        async def finish():
            entered.set()
            await release.wait()
            await cleanup()

        monkeypatch.setattr(environment, "cleanup", finish)
        return "ready"

    async def flow(ctx, inputs):
        task = asyncio.create_task(ctx.candidate_workflow(edit, inputs, label="report", budget=10000))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert Path(candidates[0].candidate_workspace, "report.txt").read_text() == "prepared report\n"
            assert isolated[0].is_dir()
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
                outcome = "cancelled"
            else:
                candidate = await task
                assert candidate.output == "ready"
                assert "+prepared report" in candidate.diff
                outcome = "finished"
            assert ctx.tokens_spent() == 15
            assert ctx.tokens_remaining() == 19985
            return outcome
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    environment = LocalEnvironment(str(repository))
    workspace = CandidateWorkspace(environment)
    try:
        result = await OpenCollab(repository, environment=environment, model="test-model").workflow(
            flow, llm=Model(), candidate_workspace=workspace, trace=False,
            limit_mode="explicit", budget=20000, timeout=10,
        )
        assert result.ok, result.reason
        assert result.output == ("cancelled" if cancel else "finished")
        assert result.tokens == 15
        candidate, = candidates
        assert not isolated[0].exists()
        worktree_list = _git(repository, "worktree", "list", "--porcelain")
        assert (
            Path(candidate.candidate_workspace).exists(),
            candidate.candidate_workspace in worktree_list,
            candidate.environment.revoked,
        ) == (False, False, True)
        assert candidate.cleaned
        assert _git(repository, "status", "--porcelain") == ""
    finally:
        for lease in reversed(candidates):
            await lease.cleanup()
        await environment.cleanup()
