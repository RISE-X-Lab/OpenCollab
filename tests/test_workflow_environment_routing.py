"""Workflow isolation must follow the supplied environment and retain cleanup ownership."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.bootstrap.workflow_runtime import WorkflowSessionFactory


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def repository(path, marker):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.test")
    (path / "marker.txt").write_text(marker)
    git(path, "add", "marker.txt")
    git(path, "commit", "-qm", "initial")
    return path


def factory(workspace=None, env=None):
    return WorkflowSessionFactory(
        model="test-model", provider="openai", api_key=None, base_url=None, workspace=workspace, env=env
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_supplied", [True, False])
async def test_isolated_role_reads_the_supplied_repository(tmp_path, anchor_supplied):
    anchor = repository(tmp_path / "anchor", "host repository")
    target = repository(tmp_path / "target", "task repository")
    owner = factory(str(anchor) if anchor_supplied else None, LocalEnvironment(str(target)))
    isolated = await owner.acquire_isolated_env(label="coder")
    try:
        assert await isolated.read_file("marker.txt") == "task repository"
        await isolated.write_file("candidate.txt", "edited")
        assert not (anchor / "candidate.txt").exists()
        assert not (target / "candidate.txt").exists()
    finally:
        await owner.release_isolated_envs()


@pytest.mark.asyncio
async def test_unsupported_supplied_environment_does_not_fall_back_to_host(tmp_path):
    anchor = repository(tmp_path / "anchor", "host repository")
    owner = factory(str(anchor), Environment())
    try:
        with pytest.raises(TypeError, match="isolation is not available"):
            await owner.acquire_isolated_env(label="coder")
    finally:
        await owner.release_isolated_envs()
    assert len(git(anchor, "worktree", "list", "--porcelain").split("worktree ")) == 2


@pytest.mark.asyncio
async def test_failed_release_keeps_worktree_available_for_cleanup_retry(tmp_path, monkeypatch):
    anchor = repository(tmp_path / "anchor", "task repository")
    owner = factory(str(anchor))
    isolated = await owner.acquire_isolated_env(label="coder")
    cleanup = isolated.cleanup
    calls = 0

    async def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary cleanup failure")
        await cleanup()

    monkeypatch.setattr(isolated, "cleanup", fail_once)
    try:
        with pytest.raises(OSError, match="cleanup failed"):
            await owner.release_isolated_envs()
        assert Path(isolated.workspace).exists()
        await owner.release_isolated_envs()
        assert calls == 2
        assert not Path(isolated.workspace).exists()
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_implicit_workspace_still_isolates_sibling_roles(tmp_path, monkeypatch):
    cwd = tmp_path / "plain-directory"
    cwd.mkdir()
    (cwd / "marker.txt").write_text("task repository")
    monkeypatch.chdir(cwd)
    owner = factory()
    first = await owner.acquire_isolated_env(label="coder")
    second = await owner.acquire_isolated_env(label="tester")
    try:
        await first.write_file("private.txt", "coder change")
        assert first.workspace != second.workspace
        assert not (Path(second.workspace) / "private.txt").exists()
        assert not (cwd / "private.txt").exists()
        assert "private.txt" in await first.get_diff()
    finally:
        await owner.release_isolated_envs()


@pytest.mark.asyncio
async def test_workspace_acquired_while_release_finishes_remains_tracked(tmp_path, monkeypatch):
    import asyncio

    anchor = repository(tmp_path / "anchor", "task repository")
    owner = factory(str(anchor))
    await owner.acquire_isolated_env(label="first")
    pool = owner._worktree_pool
    released = asyncio.Event()
    finish = asyncio.Event()
    original = pool.release

    async def pause_after_release():
        await original()
        released.set()
        await finish.wait()

    monkeypatch.setattr(pool, "release", pause_after_release)
    task = asyncio.create_task(owner.release_isolated_envs())
    await released.wait()
    next_workspace = None
    try:
        next_workspace = await owner.acquire_isolated_env(label="next")
        finish.set()
        await task
        await owner.release_isolated_envs()
        assert not Path(next_workspace.workspace).exists()
    finally:
        finish.set()
        await task
        if next_workspace is not None:
            await next_workspace.cleanup()
