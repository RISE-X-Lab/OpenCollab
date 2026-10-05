from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_candidates import CandidateWorkspaceTrackingError
from tests.support.workflow_context_test_support import FakeFactory, FakeSession


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


@pytest.mark.parametrize("mode", ["agent", "workflow"])
@pytest.mark.asyncio
async def test_post_capture_source_read_error_preserves_candidate_lease(tmp_path, monkeypatch, mode):
    repo = _repository(tmp_path)
    base = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(base)
    leases = []
    acquire = workspace.acquire
    source_diff = workspace.source_diff
    reads = 0

    async def recorded_acquire(label):
        lease = await acquire(label)
        leases.append(lease)
        return lease

    async def fail_post_capture(exclude_paths=()):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("source read unavailable")
        return await source_diff(exclude_paths)

    monkeypatch.setattr(workspace, "acquire", recorded_acquire)
    monkeypatch.setattr(workspace, "source_diff", fail_post_capture)
    ctx = WorkflowContext(
        FakeFactory([FakeSession()]),
        budget_total=None,
        candidate_workspace=workspace,
    )

    async def nested(child, _args):
        return await child.agent("candidate work")

    try:
        with pytest.raises(CandidateWorkspaceTrackingError, match="could not verify source worktree") as captured:
            if mode == "agent":
                await ctx.candidate_agent("edit", label="candidate")
            else:
                await ctx.candidate_workflow(nested, {}, label="candidate")
        assert isinstance(captured.value.__cause__, OSError)
        assert "candidate patch was captured" in str(captured.value)
        assert Path(leases[0].candidate_workspace).is_dir()
    finally:
        for lease in leases:
            await lease.cleanup()


@pytest.mark.parametrize("mode", ["agent", "workflow"])
@pytest.mark.asyncio
async def test_cancelled_source_verification_preserves_candidate_lease(tmp_path, monkeypatch, mode):
    repo = _repository(tmp_path)
    base = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(base)
    leases = []
    acquire = workspace.acquire
    source_diff = workspace.source_diff
    reads = 0

    async def recorded_acquire(label):
        lease = await acquire(label)
        leases.append(lease)
        return lease

    async def cancel_post_capture(exclude_paths=()):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise asyncio.CancelledError
        return await source_diff(exclude_paths)

    monkeypatch.setattr(workspace, "acquire", recorded_acquire)
    monkeypatch.setattr(workspace, "source_diff", cancel_post_capture)
    ctx = WorkflowContext(
        FakeFactory([FakeSession()]),
        budget_total=None,
        candidate_workspace=workspace,
    )

    async def nested(child, _args):
        return await child.agent("candidate work")

    try:
        with pytest.raises(asyncio.CancelledError):
            if mode == "agent":
                await ctx.candidate_agent("edit", label="candidate")
            else:
                await ctx.candidate_workflow(nested, {}, label="candidate")
        assert Path(leases[0].candidate_workspace).is_dir()
    finally:
        for lease in leases:
            await lease.cleanup()
