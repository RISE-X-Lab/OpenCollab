"""Behavioral tests for worktree isolation and pool ownership."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from opencollab.adapters import worktree_pool as pool_module
from opencollab.adapters.env import ExecResult, LocalEnvironment, WorktreeEnvironment
from opencollab.adapters.worktree_pool import WorktreePool


def _git(repo, *args, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _branch_exists(repo, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


async def test_pool_without_worktrees_returns_local_environment(tmp_path) -> None:
    pool = WorktreePool(str(tmp_path), use_worktrees=False)
    env = await pool.acquire("coder")
    assert isinstance(env, LocalEnvironment)
    assert pool._envs == []


async def test_pool_rejects_unsafe_role_before_creating_workspace(tmp_path) -> None:
    pool = WorktreePool(str(tmp_path), use_worktrees=True)
    with pytest.raises(ValueError, match="role"):
        await pool.acquire("../../../outside")
    assert list(tmp_path.iterdir()) == []


async def test_non_git_source_uses_independent_directory_copy(tmp_path) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    (source / "note.txt").write_text("source", encoding="utf-8")
    env = WorktreeEnvironment(str(source), branch_name="plain-copy")
    workspace = await env.setup()
    assert workspace != str(source)
    assert not env.process_isolated
    await env.write_file("note.txt", "child")
    assert (source / "note.txt").read_text(encoding="utf-8") == "source"
    assert await env.read_file("note.txt") == "child"
    await env.cleanup()
    assert not os.path.exists(workspace)


async def test_git_worktree_reports_committed_and_untracked_changes(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    branch = "opencollab-test-diff"
    env = WorktreeEnvironment(str(source), branch_name=branch)
    workspace = await env.setup()
    await env.write_file("tracked.txt", "changed\n")
    await env.write_file("new.txt", "new\n")
    result = await env.exec_cmd("git add tracked.txt && git commit -qm child")
    assert result.returncode == 0
    diff = await env.get_diff()
    assert "tracked.txt" in diff
    assert "new.txt" in diff
    await env.cleanup()
    assert not os.path.exists(workspace)
    assert not _branch_exists(source, branch)


async def test_existing_branch_is_preserved_when_setup_fails(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    branch = "existing"
    _git(source, "branch", branch)
    env = WorktreeEnvironment(str(source), branch_name=branch)
    with pytest.raises(RuntimeError, match="already exists"):
        await env.setup()
    assert _branch_exists(source, branch)


async def test_failed_worktree_add_removes_unregistered_directory(tmp_path, monkeypatch) -> None:
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="failed-add")
    real_git = env._git

    async def fail_add(*args, **kwargs):
        if args[:2] == ("worktree", "add"):
            return ExecResult(1, "", "injected add failure")
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(env, "_git", fail_add)
    with pytest.raises(RuntimeError, match="injected add failure"):
        await env.setup()
    assert env._worktree_dir is None
    assert not _branch_exists(source, "failed-add")


async def test_double_cancellation_cannot_interrupt_setup_cleanup(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    env = WorktreeEnvironment(str(source), branch_name="cancelled-setup")
    setup_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def fake_git(*_args, **_kwargs):
        return ExecResult(0, ".git\n", "")

    async def blocked_setup():
        setup_started.set()
        await asyncio.Event().wait()

    async def cleanup():
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_done.set()

    monkeypatch.setattr(env, "_git", fake_git)
    monkeypatch.setattr(env, "_setup_git_worktree", blocked_setup)
    monkeypatch.setattr(env, "_cleanup_resources", cleanup)
    owner = asyncio.create_task(env.setup())
    await setup_started.wait()
    owner.cancel()
    await cleanup_started.wait()
    owner.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cleanup_done.is_set()


async def test_pool_releases_all_owned_worktrees(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    pool = WorktreePool(str(source), use_worktrees=True)
    first = await pool.acquire("coder")
    second = await pool.acquire("reviewer")
    workspaces = (first.workspace, second.workspace)
    assert len(pool._envs) == 2
    await pool.release()
    assert pool._envs == []
    assert all(not os.path.exists(path) for path in workspaces)


async def test_pool_keeps_failed_cleanup_for_retry(tmp_path, monkeypatch) -> None:
    class FailingEnvironment:
        def __init__(self, *_args, **_kwargs):
            self.workspace = str(tmp_path / "partial")

        async def setup(self):
            return self.workspace

        async def cleanup(self):
            raise OSError("cleanup failed")

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", FailingEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)
    env = await pool.acquire("coder")
    with pytest.raises(OSError, match="retry state retained"):
        await pool.release()
    assert pool._envs == [env]


async def test_pool_finishes_cleanup_before_forwarding_cancellation(tmp_path, monkeypatch) -> None:
    release = asyncio.Event()
    cleaned = asyncio.Event()

    class SlowEnvironment:
        def __init__(self, *_args, **_kwargs):
            self.workspace = str(tmp_path / "slow")

        async def setup(self):
            return self.workspace

        async def cleanup(self):
            await release.wait()
            cleaned.set()

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", SlowEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)
    await pool.acquire("coder")
    owner = asyncio.create_task(pool.release())
    await asyncio.sleep(0)
    owner.cancel()
    await asyncio.sleep(0)
    assert not owner.done()
    owner.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cleaned.is_set()
