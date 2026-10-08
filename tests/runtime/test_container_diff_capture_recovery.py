"""Failed container diff capture retains a directly recoverable Git worktree."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from opencollab.adapters._env_container_worktree import ContainerWorktreeEnvironment
from opencollab.adapters._env_worktree import WorktreeEnvironment
from tests.support.worktree_delivery_support import CONTAINER_ID, apply_and_execute, git, make_repo
from tests.support.worktree_delivery_support import worktree_transport as worktree_transport


@pytest.mark.parametrize(
    "failure", ["ignored", "stdout", "stderr", "command", "metadata", "base", "cancelled"],
)
@pytest.mark.parametrize("environment", ["container", "local"])
async def test_failed_capture_retains_resources_until_successful_export(
    worktree_transport, tmp_path, monkeypatch, failure, environment,
):
    repo = make_repo(tmp_path / "repo")
    branch = "capture-recovery"
    if environment == "container":
        env = ContainerWorktreeEnvironment(
            container_id=CONTAINER_ID, repository_root=str(repo),
            worktree_root=str(tmp_path / "worktrees"), branch_name=branch,
        )
    else:
        env = WorktreeEnvironment(str(repo), branch_name=branch)
    workspace = Path(await env.setup())
    await env.write_file("solution.py", "print(42)\n")
    executor = env if environment == "container" else env._local_env
    original_exec = executor.exec_cmd
    original_base = env._base_commit
    original_resolve = env._resolve_diff_base

    async def faulted_exec(*args, **kwargs):
        if failure == "command":
            raise OSError("transport failed")
        if failure == "cancelled":
            raise asyncio.CancelledError
        result = await original_exec(*args, **kwargs)
        setattr(result, f"{failure}_truncated", True)
        return result

    async def faulted_metadata():
        raise OSError("metadata failed")

    if failure == "ignored":
        cache = workspace / "__pycache__"
        cache.mkdir()
        (cache / "solution.pyc").write_bytes(b"cache")
    elif failure == "metadata":
        monkeypatch.setattr(env, "_resolve_diff_base", faulted_metadata)
    elif failure == "base":
        env._base_commit = None
    else:
        monkeypatch.setattr(executor, "exec_cmd", faulted_exec)

    try:
        expected_error = asyncio.CancelledError if failure == "cancelled" else (RuntimeError, OSError)
        with pytest.raises(expected_error):
            await env.get_diff()
        for _ in range(2):
            with pytest.raises(RuntimeError, match="undelivered.*worktree"):
                await env.cleanup()
            assert (workspace / "solution.py").read_text() == "print(42)\n"
            assert f"worktree {workspace.resolve()}" in git(repo, "worktree", "list", "--porcelain", "-z")
            assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
        assert not env.revoked
    finally:
        monkeypatch.setattr(executor, "exec_cmd", original_exec)
        monkeypatch.setattr(env, "_resolve_diff_base", original_resolve)
        env._base_commit = original_base
        if failure == "ignored":
            shutil.rmtree(workspace / "__pycache__")

    patch = await env.get_diff()
    apply_and_execute(repo, patch)
    await env.cleanup()
    await env.cleanup()
    assert not workspace.exists()
    assert branch not in git(repo, "branch", "--list")


async def test_successful_empty_capture_releases_resources(worktree_transport, tmp_path):
    repo = make_repo(tmp_path / "repo")
    env = ContainerWorktreeEnvironment(
        container_id=CONTAINER_ID, repository_root=str(repo),
        worktree_root=str(tmp_path / "worktrees"), branch_name="empty-capture",
    )
    workspace = Path(await env.setup())
    assert await env.get_diff() == ""
    await env.cleanup()
    await env.cleanup()
    assert not workspace.exists()
    assert "empty-capture" not in git(repo, "branch", "--list")
