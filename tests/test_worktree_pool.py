"""Behavioral tests for worktree isolation and pool ownership."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

import pytest

from opencollab.adapters import _env_worktree as worktree_module
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


class _OneShotAsyncBarrier:
    """Release all waiters once the expected parties have arrived."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._released = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived == self._parties:
            self._released.set()
        await self._released.wait()


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
    assert "child" in await env.get_diff()
    await env.cleanup()
    assert not os.path.exists(workspace)


async def test_concurrent_setup_reuses_one_worktree(tmp_path, monkeypatch) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    env = WorktreeEnvironment(str(source), branch_name="plain-concurrent")
    started = asyncio.Event()
    release = asyncio.Event()
    probe_count = 0
    copy_count = 0

    async def blocking_probe(*args, **_kwargs):
        nonlocal probe_count
        assert args == ("rev-parse", "--git-dir")
        probe_count += 1
        started.set()
        await release.wait()
        return ExecResult(1, "", "")

    async def fake_copy() -> None:
        nonlocal copy_count
        copy_count += 1
        baseline = tmp_path / f"baseline-{copy_count}"
        worktree = tmp_path / f"worktree-{copy_count}"
        baseline.mkdir()
        worktree.mkdir()
        env._copy_baseline_dir = str(baseline)
        env._worktree_dir = str(worktree)

    monkeypatch.setattr(env, "_git", blocking_probe)
    monkeypatch.setattr(env, "_setup_directory_copy", fake_copy)
    first = asyncio.create_task(env.setup())
    await started.wait()
    second = asyncio.create_task(env.setup())
    try:
        await asyncio.sleep(0.01)
        assert probe_count == 1
    finally:
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert copy_count == 1
    assert outcomes == [env.workspace, env.workspace]


async def test_non_git_copy_does_not_block_event_loop(tmp_path, monkeypatch) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    (source / "note.txt").write_text("source", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    original_copytree = worktree_module.shutil.copytree

    def slow_copytree(*args, **kwargs):
        started.set()
        assert release.wait(1.0)
        return original_copytree(*args, **kwargs)

    monkeypatch.setattr(worktree_module.shutil, "copytree", slow_copytree)
    env = WorktreeEnvironment(str(source), branch_name="plain-async-copy")
    owner = asyncio.create_task(env.setup())
    timer = threading.Timer(0.15, release.set)
    timer.start()
    try:
        await asyncio.sleep(0.02)
        assert started.is_set()
        assert not owner.done()
    finally:
        release.set()
        timer.cancel()
    await owner
    await env.cleanup()


async def test_non_git_cleanup_does_not_block_event_loop(tmp_path, monkeypatch) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    env = WorktreeEnvironment(str(source), branch_name="plain-async-cleanup")
    await env.setup()
    started = threading.Event()
    release = threading.Event()
    original_rmtree = worktree_module.shutil.rmtree

    def slow_rmtree(*args, **kwargs):
        started.set()
        assert release.wait(1.0)
        return original_rmtree(*args, **kwargs)

    monkeypatch.setattr(worktree_module.shutil, "rmtree", slow_rmtree)
    owner = asyncio.create_task(env.cleanup())
    timer = threading.Timer(0.15, release.set)
    timer.start()
    try:
        await asyncio.sleep(0.02)
        assert started.is_set()
        assert not owner.done()
    finally:
        release.set()
        timer.cancel()
    await owner


async def test_non_git_copy_preserves_file_and_directory_symlinks(tmp_path) -> None:
    source = tmp_path / "plain"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (source / "file-link").symlink_to(outside / "secret.txt")
    (source / "dir-link").symlink_to(outside, target_is_directory=True)
    env = WorktreeEnvironment(str(source), branch_name="plain-links")

    workspace = await env.setup()

    assert os.path.islink(os.path.join(workspace, "file-link"))
    assert os.path.islink(os.path.join(workspace, "dir-link"))
    with pytest.raises(OSError):
        await env.read_file("file-link")
    with pytest.raises(OSError):
        await env.read_file("dir-link/secret.txt")
    await env.cleanup()


async def test_non_git_copy_exports_changes_before_cleanup(tmp_path) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    (source / "changed.txt").write_text("before\n", encoding="utf-8")
    (source / "removed.txt").write_text("remove me\n", encoding="utf-8")
    env = WorktreeEnvironment(str(source), branch_name="plain-export")
    workspace = await env.setup()
    await env.write_file("changed.txt", "after\n")
    (Path(workspace) / "removed.txt").unlink()
    (Path(workspace) / "added.txt").write_text("added\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexported non-Git worktree changes"):
        await env.cleanup()
    assert os.path.exists(workspace)

    diff = await env.get_diff()
    assert "changed.txt" in diff
    assert "removed.txt" in diff
    assert "added.txt" in diff
    assert "-before" in diff
    assert "+after" in diff
    assert "opencollab-cp-" not in diff

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


async def test_git_worktree_rejects_dirty_source_snapshot(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    (source / "deleted.txt").write_text("delete\n", encoding="utf-8")
    _git(source, "add", "deleted.txt")
    _git(source, "commit", "-qm", "add deletion fixture")
    (source / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(source, "add", "staged.txt")
    (source / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (source / "deleted.txt").unlink()
    branch = "dirty-source"
    env = WorktreeEnvironment(str(source), branch_name=branch)

    try:
        with pytest.raises(RuntimeError, match="uncommitted changes") as captured:
            await env.setup()
    finally:
        if env._local_env is not None:
            await env.cleanup()

    detail = str(captured.value)
    assert "staged.txt" in detail
    assert "tracked.txt" in detail
    assert "untracked.txt" in detail
    assert "deleted.txt" in detail
    assert not _branch_exists(source, branch)


async def test_git_worktree_preserves_ignored_output_until_recovered(
    tmp_path,
) -> None:
    source = _repo(tmp_path / "repo")
    (source / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-qm", "add ignore rule")
    env = WorktreeEnvironment(str(source), branch_name="ignored-output")
    workspace = Path(await env.setup())
    (workspace / "lost.ignored").write_text("deliver me\n", encoding="utf-8")
    (workspace / "shown.txt").write_text("shown\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ignored untracked files"):
        await env.get_diff()
    with pytest.raises(RuntimeError, match="worktree retained"):
        await env.cleanup()

    assert workspace.is_dir()
    assert (workspace / "lost.ignored").read_text(encoding="utf-8") == "deliver me\n"
    (workspace / "lost.ignored").unlink()
    assert "shown.txt" in await env.get_diff()
    await env.cleanup()
    assert not workspace.exists()


async def test_git_worktree_preserves_oversized_diff_until_recovered(
    tmp_path,
) -> None:
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="oversized-output")
    workspace = Path(await env.setup())
    (workspace / "large.txt").write_text("x" * (1_200_000), encoding="utf-8")

    with pytest.raises(RuntimeError, match="exceeded capture limit"):
        await env.get_diff()
    with pytest.raises(RuntimeError, match="worktree retained"):
        await env.cleanup()

    assert workspace.is_dir()
    assert (workspace / "large.txt").stat().st_size == 1_200_000
    (workspace / "large.txt").unlink()
    assert await env.get_diff() == ""
    await env.cleanup()
    assert not workspace.exists()


async def test_git_worktree_preserves_nested_source_workspace(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    nested = source / "packages" / "worker"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", "packages/worker/module.py")
    _git(source, "commit", "-qm", "add nested package")
    env = WorktreeEnvironment(str(nested), branch_name="nested-source")

    workspace = Path(await env.setup())

    assert workspace.name == "worker"
    assert (workspace / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (workspace / "packages").exists()
    await env.write_file("module.py", "VALUE = 2\n")
    assert (nested / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "packages/worker/module.py" in await env.get_diff()
    await env.cleanup()


async def test_git_worktree_initializes_source_available_submodules(tmp_path) -> None:
    dependency = _repo(tmp_path / "dependency")
    source = _repo(tmp_path / "repo")
    _git(
        source,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dependency),
        "vendor/dependency",
    )
    _git(source, "commit", "-qam", "add local dependency")
    assert (source / "vendor" / "dependency" / "tracked.txt").is_file()
    env = WorktreeEnvironment(str(source), branch_name="local-submodule")

    workspace = Path(await env.setup())

    assert (workspace / "vendor" / "dependency" / "tracked.txt").read_text(
        encoding="utf-8"
    ) == "base\n"
    await env.cleanup()


async def test_git_worktree_maps_nested_submodules_to_initialized_source(
    tmp_path,
    monkeypatch,
) -> None:
    leaf = _repo(tmp_path / "leaf")
    middle = _repo(tmp_path / "middle")
    _git(
        middle,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(leaf),
        "vendor/leaf",
    )
    _git(
        middle,
        "config",
        "-f",
        ".gitmodules",
        "submodule.vendor/leaf.url",
        "https://example.invalid/leaf.git",
    )
    _git(middle, "add", ".gitmodules", "vendor/leaf")
    _git(middle, "commit", "-qm", "add nested leaf")

    source = _repo(tmp_path / "repo")
    _git(
        source,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(middle),
        "modules/middle",
    )
    _git(
        source / "modules" / "middle",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=always",
        "-c",
        f"submodule.vendor/leaf.url={leaf}",
        "submodule",
        "update",
        "--init",
        "--no-fetch",
        "--",
        "vendor/leaf",
    )
    _git(
        source,
        "config",
        "-f",
        ".gitmodules",
        "submodule.modules/middle.url",
        "https://example.invalid/middle.git",
    )
    _git(source, "add", ".gitmodules", "modules/middle")
    _git(source, "commit", "-qm", "add nested dependency")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    env = WorktreeEnvironment(str(source), branch_name="nested-local-submodule")

    workspace = Path(await env.setup())

    assert (
        workspace / "modules" / "middle" / "vendor" / "leaf" / "tracked.txt"
    ).read_text(encoding="utf-8") == "base\n"
    await env.cleanup()


async def test_git_worktree_rejects_truncated_patch_evidence(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="truncated-diff")
    await env.setup()

    async def truncated_exec(*_args, **_kwargs):
        return ExecResult(0, "partial diff", "", stdout_truncated=True)

    assert env._local_env is not None
    real_exec = env._local_env.exec_cmd
    env._local_env.exec_cmd = truncated_exec
    with pytest.raises(RuntimeError, match="exceeded capture limit"):
        await env.get_diff()
    with pytest.raises(RuntimeError, match="worktree retained"):
        await env.cleanup()
    env._local_env.exec_cmd = real_exec
    assert await env.get_diff() == ""
    await env.cleanup()


async def test_git_worktree_reports_returncode_when_diff_has_no_stderr(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="opencollab-test-empty-error")
    await env.setup()

    async def failed_exec(*_args, **_kwargs):
        return ExecResult(128, "", "")

    assert env._local_env is not None
    real_exec = env._local_env.exec_cmd
    env._local_env.exec_cmd = failed_exec
    with pytest.raises(RuntimeError, match="git exited with status 128"):
        await env.get_diff()
    with pytest.raises(RuntimeError, match="worktree retained"):
        await env.cleanup()
    env._local_env.exec_cmd = real_exec
    assert await env.get_diff() == ""
    await env.cleanup()


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


async def test_concurrent_same_branch_setup_preserves_winner_after_loser_cleanup(tmp_path, monkeypatch) -> None:
    source = _repo(tmp_path / "repo")
    branch = "same-branch"
    first = WorktreeEnvironment(str(source), branch_name=branch)
    second = WorktreeEnvironment(str(source), branch_name=branch)
    barrier = _OneShotAsyncBarrier(2)

    def make_racing_git(env):
        real_git = env._git
        paused = False

        async def racing_git(*args, **kwargs):
            nonlocal paused
            if not paused and args[:2] == ("show-ref", "--verify"):
                paused = True
                await barrier.wait()
            return await real_git(*args, **kwargs)

        return racing_git

    monkeypatch.setattr(first, "_git", make_racing_git(first))
    monkeypatch.setattr(second, "_git", make_racing_git(second))
    outcomes = await asyncio.gather(first.setup(), second.setup(), return_exceptions=True)

    winners = [env for env, outcome in zip((first, second), outcomes) if isinstance(outcome, str)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1
    assert len(failures) == 1
    winner = winners[0]
    assert _branch_exists(source, branch)
    assert os.path.isdir(winner.workspace)
    assert os.path.exists(os.path.join(winner.workspace, "tracked.txt"))

    await winner.cleanup()
    assert not _branch_exists(source, branch)


async def test_worktree_cleanup_preserves_externally_advanced_owned_branch(tmp_path) -> None:
    source = _repo(tmp_path / "repo")
    branch = "externally-advanced"
    env = WorktreeEnvironment(str(source), branch_name=branch)
    workspace = await env.setup()
    base = _git(source, "rev-parse", branch).stdout.strip()
    tree = _git(source, "rev-parse", f"{base}^{{tree}}").stdout.strip()
    advanced = _git(source, "commit-tree", tree, "-p", base, "-m", "external advance").stdout.strip()
    _git(source, "update-ref", f"refs/heads/{branch}", advanced, base)

    await env.cleanup()

    assert not os.path.exists(workspace)
    assert _branch_exists(source, branch)
    assert _git(source, "rev-parse", branch).stdout.strip() == advanced


async def test_worktree_cleanup_stops_when_local_environment_is_not_quiescent(
    tmp_path,
    monkeypatch,
) -> None:
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="retry-local-cleanup")
    workspace = await env.setup()
    assert env._local_env is not None
    local_env = env._local_env
    real_cleanup = local_env.cleanup
    git_calls: list[tuple[str, ...]] = []
    real_git = env._git

    async def track_git(*args, **kwargs):
        git_calls.append(args)
        return await real_git(*args, **kwargs)

    async def fail_cleanup():
        raise RuntimeError("process still alive")

    monkeypatch.setattr(env, "_git", track_git)
    monkeypatch.setattr(local_env, "cleanup", fail_cleanup)

    with pytest.raises(RuntimeError, match="worktree cleanup failed"):
        await env.cleanup()

    assert os.path.isdir(workspace)
    assert env._local_env is local_env
    assert not any(args[:2] == ("worktree", "remove") for args in git_calls)

    monkeypatch.setattr(local_env, "cleanup", real_cleanup)
    await env.cleanup()
    assert not os.path.exists(workspace)


async def test_double_cancellation_cannot_interrupt_setup_cleanup(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    env = WorktreeEnvironment(str(source), branch_name="cancelled-setup")
    setup_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def fake_git(*args, **_kwargs):
        if args[-1:] == ("--show-toplevel",):
            return ExecResult(0, str(source), "")
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
