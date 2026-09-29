"""Recovery after real Git worktree removal failures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.env import ExecResult, WorktreeEnvironment


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    source = tmp_path / "repo"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "tests@example.com")
    _git(source, "config", "user.name", "OpenCollab Tests")
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-qm", "base")
    return source


def _registered(repo: Path, workspace: Path) -> bool:
    listing = _git(repo, "worktree", "list", "--porcelain", "-z").stdout
    return f"worktree {workspace.resolve()}" in listing.split("\0")


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="requires enforced POSIX permissions")
@pytest.mark.parametrize("operation", ["cleanup", "abort"])
async def test_permission_failure_unregisters_worktree_and_cleanup_can_retry(repo, monkeypatch, operation):
    env = WorktreeEnvironment(str(repo), branch_name="permission-recovery")
    workspace = Path(await env.setup())
    protected = workspace / "protected"
    protected.mkdir()
    (protected / "output.txt").write_text("output\n", encoding="utf-8")
    protected.chmod(0o500)
    removals: list[ExecResult] = []
    real_git = env._git

    async def record_git(*args, **kwargs):
        result = await real_git(*args, **kwargs)
        if args[:2] == ("worktree", "remove"):
            removals.append(result)
        return result

    monkeypatch.setattr(env, "_git", record_git)
    try:
        with pytest.raises(RuntimeError, match="worktree cleanup failed"):
            await getattr(env, operation)()
        # The command really failed, yet Git already removed its registration.
        assert len(removals) == 1 and removals[0].returncode != 0
        assert "Permission denied" in removals[0].stderr
        assert not _registered(repo, workspace)
        assert protected.is_dir()
        assert not env._worktree_registered
        assert not _branch_exists(repo, "permission-recovery")
    finally:
        protected.chmod(0o700)
        await env.cleanup()
    assert not workspace.exists()
    assert not _branch_exists(repo, "permission-recovery")
    assert len(removals) == 1  # Retry only removes the residual directory.
    await env.cleanup()  # Successful cleanup is idempotent.


async def test_locked_worktree_retains_directory_and_branch_until_unlocked(repo, tmp_path, monkeypatch):
    temporary_root = tmp_path / "worktrees with spaces\nand unicode \u5de5\u4f5c"
    temporary_root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temporary_root))
    env = WorktreeEnvironment(str(repo), branch_name="locked-recovery")
    workspace = Path(await env.setup())
    _git(repo, "worktree", "lock", str(workspace))
    try:
        with pytest.raises(RuntimeError, match="worktree cleanup failed"):
            await env.cleanup()
        assert _registered(repo, workspace)
        assert env._worktree_registered
        assert (workspace / "tracked.txt").read_text() == "base\n"
        assert _branch_exists(repo, "locked-recovery")
    finally:
        _git(repo, "worktree", "unlock", str(workspace))
        await env.cleanup()
    assert not workspace.exists()
    assert not _branch_exists(repo, "locked-recovery")


@pytest.mark.parametrize(
    "probe_result",
    [ExecResult(1, "", "probe failed"), ExecResult(0, "", "", stdout_truncated=True),
     ExecResult(0, "", "", stderr_truncated=True)],
)
async def test_failed_registration_probe_cannot_authorize_directory_or_branch_deletion(repo, monkeypatch, probe_result):
    env = WorktreeEnvironment(str(repo), branch_name="unknown-registration")
    workspace = Path(await env.setup())
    _git(repo, "worktree", "lock", str(workspace))
    real_git = env._git

    async def fail_probe(*args, **kwargs):
        if args[:2] == ("worktree", "list"):
            return probe_result
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(env, "_git", fail_probe)
    try:
        with pytest.raises(RuntimeError, match="worktree cleanup failed"):
            await env.cleanup()
        assert _registered(repo, workspace)
        assert (workspace / "tracked.txt").exists()
        assert _branch_exists(repo, "unknown-registration")
    finally:
        monkeypatch.setattr(env, "_git", real_git)
        _git(repo, "worktree", "unlock", str(workspace))
        await env.cleanup()
