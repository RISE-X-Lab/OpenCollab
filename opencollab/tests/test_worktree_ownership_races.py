"""Worktree directory and branch ownership race regressions."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import opencollab.adapters._env_pinned_git as pinned_git_module
import opencollab.adapters.env as env_module
import pytest
from opencollab.adapters.env import ExecResult, WorktreeEnvironment


def _init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "audit@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Audit"], cwd=path, check=True)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def _seed_replacement_repo(path: Path, moved_source: Path, branch: str, oid: str) -> Path:
    replacement = _init_repo(path)
    subprocess.run(
        ["git", "fetch", "-q", str(moved_source), oid],
        cwd=replacement,
        check=True,
    )
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{branch}", oid],
        cwd=replacement,
        check=True,
    )
    return replacement


def _repo_ownership_snapshot(repo: Path) -> tuple[str, str]:
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return refs, worktrees


def test_cleanup_uses_pinned_source_after_repository_path_replacement(tmp_path):
    source = _init_repo(tmp_path / "source")
    branch = "pinned-source-cleanup"
    env = WorktreeEnvironment(str(source), branch_name=branch)
    workspace = Path(asyncio.run(env.setup()))
    owned_oid = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{branch}"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    moved_source = tmp_path / "moved-source"
    source.rename(moved_source)
    replacement = _seed_replacement_repo(source, moved_source, branch, owned_oid)
    replacement_before = _repo_ownership_snapshot(replacement)

    with pytest.raises(OSError, match="worktree remove failed"):
        asyncio.run(env.cleanup())
    asyncio.run(env.cleanup())

    assert env._source_handle.fd == -1
    assert not workspace.exists()
    assert _repo_ownership_snapshot(replacement) == replacement_before
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=moved_source,
        check=False,
    ).returncode == 1


def test_late_compensation_uses_pinned_source_after_path_replacement(tmp_path):
    source = _init_repo(tmp_path / "source")
    branch = "pinned-source-late-compensation"
    env = WorktreeEnvironment(str(source), branch_name=branch)
    workspace = Path(asyncio.run(env.setup()))
    owned_oid = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{branch}"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    moved_source = tmp_path / "moved-source"
    source.rename(moved_source)
    replacement = _seed_replacement_repo(source, moved_source, branch, owned_oid)
    replacement_before = _repo_ownership_snapshot(replacement)

    with pytest.raises(OSError, match="late git worktree remove failed"):
        env._late_compensate_worktree_add()
    assert _repo_ownership_snapshot(replacement) == replacement_before
    assert not workspace.exists()
    assert env._branch_cleanup_pending is True
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=moved_source,
        check=False,
    ).returncode == 0

    asyncio.run(env.cleanup())
    assert env._source_handle.fd == -1
    assert _repo_ownership_snapshot(replacement) == replacement_before
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=moved_source,
        check=False,
    ).returncode == 1


def test_abort_releases_pinned_source_handle_idempotently(tmp_path):
    source = _init_repo(tmp_path / "source")
    branch = "pinned-source-abort"
    env = WorktreeEnvironment(str(source), branch_name=branch)
    workspace = Path(asyncio.run(env.setup()))

    asyncio.run(env.abort())
    asyncio.run(env.abort())
    asyncio.run(env.cleanup())

    assert env._source_handle.fd == -1
    assert not workspace.exists()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source,
        check=False,
    ).returncode == 1


def test_worktree_remove_rejects_unknown_path_without_touching_victim(tmp_path):
    source = _init_repo(tmp_path / "source")
    env = WorktreeEnvironment(str(source), branch_name="unknown-remove-guard")
    workspace = Path(asyncio.run(env.setup()))
    victim = workspace / "victim.txt"
    victim.write_text("preserve", encoding="utf-8")

    with pytest.raises(OSError, match="without temporary ownership proof"):
        asyncio.run(env.remove_file("victim.txt"))

    assert victim.read_text(encoding="utf-8") == "preserve"
    asyncio.run(env.cleanup())


def test_source_handle_close_failure_retains_cleanup_retry_state(tmp_path, monkeypatch):
    source = _init_repo(tmp_path / "source")
    env = WorktreeEnvironment(str(source), branch_name="source-handle-close-retry")
    workspace = Path(asyncio.run(env.setup()))
    source_fd = env._source_handle.fd
    original_close = pinned_git_module.os.close
    failed_once = False

    def fail_source_close_once(fd):
        nonlocal failed_once
        if fd == source_fd and not failed_once:
            failed_once = True
            raise OSError("simulated source descriptor close failure")
        return original_close(fd)

    monkeypatch.setattr(pinned_git_module.os, "close", fail_source_close_once)
    with pytest.raises(OSError, match="source repository handle cleanup failed"):
        asyncio.run(env.cleanup())

    assert failed_once is True
    assert env._source_handle.fd == source_fd
    assert not workspace.exists()

    asyncio.run(env.cleanup())
    assert env._source_handle.fd == -1


def test_quarantine_preserves_foreign_directory_created_at_original_path(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(repo), branch_name="quarantine-original-race")
    workspace = Path(asyncio.run(env.setup()))
    original_run_git = env._run_git
    remove_failed = False

    async def fail_registered_remove_once(*args, **kwargs):
        nonlocal remove_failed
        if args[:3] == ("worktree", "remove", "--force") and not remove_failed:
            remove_failed = True
            return ExecResult(1, "", "force fallback removal")
        return await original_run_git(*args, **kwargs)

    original_rmdir = env_module._env_worktree.os.rmdir
    injected = False

    def create_foreign_at_final_quarantine_remove(path, *args, **kwargs):
        nonlocal injected
        if not injected and isinstance(path, str) and path.startswith(".opencollab-remove-"):
            injected = True
            workspace.mkdir()
            (workspace / "foreign-data").write_text("preserve", encoding="utf-8")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(env, "_run_git", fail_registered_remove_once)
    monkeypatch.setattr(env_module._env_worktree.os, "rmdir", create_foreign_at_final_quarantine_remove)
    with pytest.raises(OSError, match="force fallback removal"):
        asyncio.run(env.cleanup())

    assert injected is True
    assert (workspace / "foreign-data").read_text(encoding="utf-8") == "preserve"
    assert env._worktree_dir is None


def test_quarantine_refuses_final_entry_exchange(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    worktree = tmp_path / "partial-worktree"
    worktree.mkdir()
    (worktree / "owned").write_text("owned", encoding="utf-8")
    env = WorktreeEnvironment(str(source), branch_name="quarantine-entry-race")
    env._worktree_dir = str(worktree)
    original_rmdir = env_module._env_worktree.os.rmdir
    moved_owned = tmp_path / "moved-owned-quarantine"
    injected = False

    def exchange_quarantine_before_final_remove(path, *args, **kwargs):
        nonlocal injected
        if not injected and isinstance(path, str) and path.startswith(".opencollab-remove-"):
            injected = True
            parent_fd = kwargs["dir_fd"]
            os.rename(path, str(moved_owned), src_dir_fd=parent_fd)
            os.mkdir(path, dir_fd=parent_fd)
            foreign_fd = os.open(path, os.O_RDONLY, dir_fd=parent_fd)
            try:
                file_fd = os.open(
                    "foreign-data",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=foreign_fd,
                )
                os.write(file_fd, b"preserve")
                os.close(file_fd)
            finally:
                os.close(foreign_fd)
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(env_module._env_worktree.os, "rmdir", exchange_quarantine_before_final_remove)
    with pytest.raises(OSError, match="Directory not empty|not empty"):
        asyncio.run(env.cleanup())

    quarantine = Path(env._worktree_quarantine_dir)
    assert (quarantine / "foreign-data").read_text(encoding="utf-8") == "preserve"
    assert moved_owned.exists()
    assert env._worktree_dir == str(worktree)

    monkeypatch.undo()
    shutil.rmtree(quarantine)
    moved_owned.rmdir()
    env._worktree_quarantine_dir = None
    env._worktree_directory_removed = True
    asyncio.run(env.cleanup())


def test_branch_compare_delete_preserves_replaced_ref(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    branch = "branch-cas-race"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    asyncio.run(env.setup())
    original_run_git = env._run_git
    foreign_oid = subprocess.run(
        ["git", "commit-tree", "HEAD^{tree}", "-m", "foreign ref"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    injected = False

    async def replace_ref_before_compare_delete(*args, **kwargs):
        nonlocal injected
        if args[:2] == ("update-ref", "-d") and not injected:
            injected = True
            subprocess.run(
                ["git", "update-ref", f"refs/heads/{branch}", foreign_oid],
                cwd=repo,
                check=True,
            )
        return await original_run_git(*args, **kwargs)

    monkeypatch.setattr(env, "_run_git", replace_ref_before_compare_delete)
    with pytest.raises(OSError, match="compare-and-delete failed"):
        asyncio.run(env.cleanup())

    assert injected is True
    assert env._branch_cleanup_pending is True
    assert env._branch_owned_oid != foreign_oid
    current = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{branch}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current == foreign_oid

    with pytest.raises(OSError, match="compare-and-delete failed"):
        asyncio.run(env.cleanup())
    subprocess.run(
        ["git", "update-ref", "-d", f"refs/heads/{branch}", foreign_oid],
        cwd=repo,
        check=True,
    )
    asyncio.run(env.cleanup())


def test_failed_worktree_removal_never_deletes_branch(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    branch = "retain-branch-after-remove-failure"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    asyncio.run(env.setup())
    original_run_git = env._run_git
    branch_delete_attempted = False

    async def fail_removal_and_prune(*args, **kwargs):
        nonlocal branch_delete_attempted
        if args[:3] == ("worktree", "remove", "--force"):
            return ExecResult(1, "", "remove failed")
        if args[:3] == ("worktree", "prune", "--expire"):
            return ExecResult(1, "", "prune failed")
        if args[:2] == ("update-ref", "-d"):
            branch_delete_attempted = True
        return await original_run_git(*args, **kwargs)

    monkeypatch.setattr(env, "_run_git", fail_removal_and_prune)
    with pytest.raises(OSError, match="remove failed"):
        asyncio.run(env.cleanup())

    assert branch_delete_attempted is False
    assert env._branch_cleanup_pending is True
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        check=False,
    ).returncode == 0

    monkeypatch.undo()
    asyncio.run(env.cleanup())
