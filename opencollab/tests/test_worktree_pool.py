"""Worktree lifecycle, retry-state, and pool ownership regressions."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading

import opencollab.adapters.env as env_module
import opencollab.adapters.worktree_pool as pool_module
import pytest
from opencollab.adapters.env import ExecResult, LocalEnvironment, WorktreeEnvironment
from opencollab.adapters.worktree_pool import WorktreePool


def _init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "audit@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Audit"],
        cwd=path,
        check=True,
    )
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def _branch_exists(repo, branch):
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def test_pool_returns_local_env_when_worktrees_disabled(tmp_path):
    pool = WorktreePool(str(tmp_path), use_worktrees=False)
    assert isinstance(asyncio.run(pool.acquire("coder")), LocalEnvironment)
    assert isinstance(asyncio.run(pool.acquire("reviewer")), LocalEnvironment)
    assert pool._envs == []
    asyncio.run(pool.release())
    asyncio.run(pool.cleanup())


def test_pool_rejects_unsafe_role_even_when_worktrees_are_disabled(tmp_path):
    pool = WorktreePool(str(tmp_path), use_worktrees=False)

    with pytest.raises(ValueError, match="role"):
        asyncio.run(pool.acquire("../../../outside"))


def test_pool_uses_digest_safe_branch_component_for_unicode_role(
    tmp_path,
    monkeypatch,
):
    created = []

    class CapturingEnvironment:
        def __init__(self, workspace, *, branch_name):
            self.workspace = workspace
            self.branch_name = branch_name
            created.append(self)

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", CapturingEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)

    asyncio.run(pool.acquire("🧪"))

    assert len(created) == 1
    assert created[0].branch_name.startswith("opencollab-role-")
    assert "/" not in created[0].branch_name
    assert "\\" not in created[0].branch_name


def test_pool_cleans_partial_environment_after_setup_cancellation(
    tmp_path,
    monkeypatch,
):
    created = []

    class PartialEnvironment:
        def __init__(self, *args, **kwargs):
            self.workspace = str(tmp_path / "partial")
            self.setup_started = asyncio.Event()
            self.cleaned = False
            created.append(self)

        async def setup(self):
            self.setup_started.set()
            await asyncio.Event().wait()

        async def cleanup(self):
            self.cleaned = True

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", PartialEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)

    async def scenario():
        task = asyncio.create_task(pool.acquire("coder"))
        while not created:
            await asyncio.sleep(0)
        await created[0].setup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert created[0].cleaned is True
    assert pool._envs == []


def test_pool_double_cancel_waits_for_partial_cleanup(tmp_path, monkeypatch):
    created = []

    class SlowCleanupEnvironment:
        def __init__(self, *args, **kwargs):
            self.workspace = str(tmp_path / "slow")
            self.setup_started = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.cleanup_release = asyncio.Event()
            self.cleaned = False
            created.append(self)

        async def setup(self):
            self.setup_started.set()
            await asyncio.Event().wait()

        async def cleanup(self):
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleaned = True

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", SlowCleanupEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)

    async def scenario():
        task = asyncio.create_task(pool.acquire("coder"))
        while not created:
            await asyncio.sleep(0)
        env = created[0]
        await env.setup_started.wait()
        task.cancel("first cancellation")
        await env.cleanup_started.wait()
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        env.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert captured.value.args == ("first cancellation",)

    asyncio.run(scenario())
    assert created[0].cleaned is True


def test_pool_retains_partial_environment_when_setup_cleanup_fails(
    tmp_path,
    monkeypatch,
):
    created = []

    class FailedPartialEnvironment:
        def __init__(self, *args, **kwargs):
            self.workspace = str(tmp_path / "failed-partial")
            self.cleanup_attempts = 0
            created.append(self)

        async def setup(self):
            raise RuntimeError("setup failed")

        async def cleanup(self):
            self.cleanup_attempts += 1
            if self.cleanup_attempts == 1:
                raise OSError("cleanup failed")

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", FailedPartialEnvironment)
    pool = WorktreePool(str(tmp_path), use_worktrees=True)

    with pytest.raises(RuntimeError, match="setup failed") as captured:
        asyncio.run(pool.acquire("coder"))
    assert any(
        "retained for cleanup retry" in note
        for note in getattr(captured.value, "__notes__", [])
    ) or not hasattr(captured.value, "add_note")
    assert pool._envs == created

    asyncio.run(pool.release())
    assert pool._envs == []


def test_pool_release_traverses_all_and_retains_only_failures(tmp_path):
    class FakeEnvironment:
        def __init__(self, name, failures):
            self.workspace = name
            self.failures = failures
            self.attempts = 0

        async def cleanup(self):
            self.attempts += 1
            if self.attempts <= self.failures:
                raise OSError(f"{self.workspace} failed")

    pool = WorktreePool(str(tmp_path), use_worktrees=True)
    good = FakeEnvironment("good", 0)
    retry = FakeEnvironment("retry", 1)
    pool._envs = [good, retry]

    with pytest.raises(OSError, match="retry state retained"):
        asyncio.run(pool.release())
    assert good.attempts == 1
    assert retry.attempts == 1
    assert pool._envs == [retry]

    asyncio.run(pool.release())
    assert retry.attempts == 2
    assert pool._envs == []


def test_pool_release_env_failure_is_retained_and_propagated(tmp_path, monkeypatch):
    class FakeWorktree:
        def __init__(self):
            self.workspace = "retry-one"
            self.attempts = 0

        async def cleanup(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("first cleanup failed")

    monkeypatch.setattr(pool_module, "WorktreeEnvironment", FakeWorktree)
    env = FakeWorktree()
    pool = WorktreePool(str(tmp_path), use_worktrees=True)
    pool._envs = [env]

    with pytest.raises(OSError, match="first cleanup failed"):
        asyncio.run(pool.release_env(env))
    assert pool._envs == [env]
    asyncio.run(pool.release_env(env))
    assert pool._envs == []


def test_worktree_diff_includes_committed_and_untracked_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")

    async def scenario():
        env = WorktreeEnvironment(str(repo), branch_name="audit-diff")
        await env.setup()
        try:
            await env.write_file("tracked.txt", "committed change\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=env.workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "child commit"],
                cwd=env.workspace,
                check=True,
            )
            await env.write_file("new_file.py", "print('untracked')\n")
            return await env.get_diff()
        finally:
            await env.cleanup()

    diff = asyncio.run(scenario())
    assert "+committed change" in diff
    assert "new_file.py" in diff
    assert "+print('untracked')" in diff


def test_existing_branch_failure_preserves_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    subprocess.run(["git", "branch", "existing-review"], cwd=repo, check=True)
    env = WorktreeEnvironment(str(repo), branch_name="existing-review")

    with pytest.raises(RuntimeError, match="already exists"):
        asyncio.run(env.setup())
    asyncio.run(env.cleanup())
    assert _branch_exists(repo, "existing-review")


def test_branch_created_after_probe_is_never_deleted_as_ours(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    branch = "raced-foreign-branch"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    original_run_git = env._run_git
    injected = False

    async def inject_foreign_branch_before_atomic_claim(*args, **kwargs):
        nonlocal injected
        if args[:1] == ("update-ref",) and not injected:
            injected = True
            subprocess.run(["git", "branch", branch], cwd=repo, check=True)
        return await original_run_git(*args, **kwargs)

    monkeypatch.setattr(env, "_run_git", inject_foreign_branch_before_atomic_claim)

    with pytest.raises(RuntimeError, match="already exists"):
        asyncio.run(env.setup())
    asyncio.run(env.cleanup())

    assert _branch_exists(repo, branch)
    assert env._worktree_dir is None


def test_non_git_workspace_is_rejected_without_starting_copy_fallback(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "plain"
    source.mkdir()
    (source / "a.txt").write_text("a")
    env = WorktreeEnvironment(str(source), branch_name="copy")

    def forbidden_copy(*args, **kwargs):
        raise AssertionError("non-Git fallback attempted a blocking directory copy")

    monkeypatch.setattr(shutil, "copytree", forbidden_copy)
    with pytest.raises(RuntimeError, match="requires a Git repository"):
        asyncio.run(env.setup())
    assert env._worktree_dir is None
    assert env._local_env is None
    asyncio.run(env.cleanup())


@pytest.mark.asyncio
async def test_worktree_directory_fallback_removal_runs_off_event_loop(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    worktree = tmp_path / "partial-worktree"
    worktree.mkdir()
    env = WorktreeEnvironment(str(source), branch_name="cleanup-worker")
    env._worktree_dir = str(worktree)
    entered = threading.Event()
    release = threading.Event()
    original_rmtree = shutil.rmtree

    def blocking_rmtree(path, ignore_errors=False):
        entered.set()
        assert release.wait(timeout=2)
        original_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", blocking_rmtree)
    cleanup_task = asyncio.create_task(env.cleanup())
    assert await asyncio.to_thread(entered.wait, 0.5)

    heartbeat = asyncio.create_task(asyncio.sleep(0))
    await asyncio.wait_for(heartbeat, timeout=0.2)
    assert cleanup_task.done() is False

    release.set()
    await asyncio.wait_for(cleanup_task, timeout=1)
    assert not worktree.exists()


@pytest.mark.asyncio
async def test_worktree_cleanup_double_cancel_finishes_prune_and_branch_delete(
    tmp_path,
    monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    branch = "cancel-cleanup-transaction"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    workspace = await env.setup()
    original_run_git = env._run_git
    remove_failed = False

    async def fail_first_registered_remove(*args, **kwargs):
        nonlocal remove_failed
        if args[:3] == ("worktree", "remove", "--force") and not remove_failed:
            remove_failed = True
            return ExecResult(1, "", "simulated remove failure")
        return await original_run_git(*args, **kwargs)

    entered = threading.Event()
    release = threading.Event()
    original_rmtree = shutil.rmtree

    def blocking_rmtree(path, ignore_errors=False):
        if os.path.realpath(path) == os.path.realpath(workspace):
            entered.set()
            assert release.wait(timeout=2)
        original_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(env, "_run_git", fail_first_registered_remove)
    monkeypatch.setattr(shutil, "rmtree", blocking_rmtree)
    cleanup_task = asyncio.create_task(env.cleanup())
    assert await asyncio.to_thread(entered.wait, 1)

    cleanup_task.cancel("first cleanup cancellation")
    await asyncio.sleep(0.02)
    assert cleanup_task.done() is False
    cleanup_task.cancel("second cleanup cancellation")
    await asyncio.sleep(0.02)
    assert cleanup_task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await asyncio.wait_for(cleanup_task, timeout=3)

    assert captured.value.args == ("first cleanup cancellation",)
    notes = getattr(captured.value, "__notes__", [])
    assert any(
        "worktree cleanup failed after cancellation" in note
        and "simulated remove failure" in note
        for note in notes
    )
    assert env._worktree_dir is None
    assert env._worktree_registered is False
    assert env._branch_cleanup_pending is False
    assert env._worktree_add_attempted is False
    assert not os.path.exists(workspace)
    assert not _branch_exists(repo, branch)

    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    registered_paths = {
        os.path.realpath(line.removeprefix("worktree "))
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }
    assert os.path.realpath(workspace) not in registered_paths


def test_branch_delete_failure_retries_after_worktree_directory_is_gone(
    tmp_path,
    monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    branch = "branch-delete-retry"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    workspace = asyncio.run(env.setup())
    original_run_git = env._run_git
    failed_once = False

    async def fail_first_delete(*args, **kwargs):
        nonlocal failed_once
        if args[:2] == ("branch", "-D") and not failed_once:
            failed_once = True
            return ExecResult(1, "", "simulated delete failure")
        return await original_run_git(*args, **kwargs)

    monkeypatch.setattr(env, "_run_git", fail_first_delete)
    with pytest.raises(OSError, match="git branch -D failed"):
        asyncio.run(env.cleanup())

    assert not os.path.exists(workspace)
    assert env._branch_cleanup_pending is True
    assert _branch_exists(repo, branch)

    asyncio.run(env.cleanup())
    assert env._branch_cleanup_pending is False
    assert not _branch_exists(repo, branch)


def test_partial_add_cleanup_failure_retains_all_retry_state(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    partial = tmp_path / "partial-worktree"
    partial.mkdir()
    env = WorktreeEnvironment(str(repo), branch_name="partial-retry")
    env._worktree_dir = str(partial)
    env._worktree_add_attempted = True
    env._worktree_registered = True
    env._branch_preexisting = False
    env._branch_cleanup_pending = True
    original_run_git = env._run_git
    original_rmtree = shutil.rmtree

    async def fail_cleanup_commands(*args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain"):
            return ExecResult(1, "", "list failed")
        if args[:3] == ("worktree", "remove", "--force"):
            return ExecResult(1, "", "remove failed")
        if args[:3] == ("worktree", "prune", "--expire"):
            return ExecResult(1, "", "prune failed")
        if args[:3] == ("show-ref", "--verify", "--quiet"):
            return ExecResult(0, "", "")
        if args[:2] == ("branch", "-D"):
            return ExecResult(1, "", "branch delete failed")
        raise AssertionError(args)

    monkeypatch.setattr(env, "_run_git", fail_cleanup_commands)
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(OSError) as captured:
        asyncio.run(env.cleanup())
    message = str(captured.value)
    assert "ownership probe failed" in message
    assert "worktree remove failed" in message
    assert "worktree directory still exists" in message
    assert "worktree prune failed" in message
    assert env._worktree_dir == str(partial)
    assert env._worktree_add_attempted is True
    assert env._worktree_registered is True
    assert env._branch_cleanup_pending is True

    monkeypatch.setattr(env, "_run_git", original_run_git)
    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    asyncio.run(env.cleanup())
    assert env._worktree_dir is None
    assert env._worktree_add_attempted is False
    assert env._branch_cleanup_pending is False


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_git_timeout_never_spawns_or_creates_tempdir(
    tmp_path,
    monkeypatch,
    invalid_timeout,
):
    spawns = 0

    def forbidden_spawn(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        raise AssertionError("invalid timeout reached Popen")

    monkeypatch.setattr(env_module, "WORKTREE_GIT_TIMEOUT_SECONDS", invalid_timeout)
    monkeypatch.setattr(env_module, "_PROCESS_POPEN", forbidden_spawn)
    env = WorktreeEnvironment(str(tmp_path), branch_name="invalid-timeout")

    with pytest.raises(ValueError, match="positive finite"):
        asyncio.run(env.setup())
    assert spawns == 0
    assert env._worktree_dir is None


def test_git_timeout_cleanup_failure_revokes_worktree(tmp_path, monkeypatch):
    env = WorktreeEnvironment(str(tmp_path), branch_name="timeout-revoke")

    async def timed_out(*args, **kwargs):
        raise env_module._OwnedProcessTimeout(cleanup_quiesced=False)

    monkeypatch.setattr(env_module, "_run_thread_owned_process", timed_out)
    with pytest.raises(RuntimeError, match="cleanup_quiesced=False"):
        asyncio.run(env.setup())
    assert env._aborted is True
    with pytest.raises(RuntimeError, match="aborted"):
        asyncio.run(env.setup())


def test_worktree_add_handoff_cancel_cleans_branch_registration_and_directory(
    tmp_path,
    monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    branch = "handoff-cancel"
    env = WorktreeEnvironment(str(repo), branch_name=branch)
    original_wait = env_module._wait_thread_event
    wait_calls = 0

    async def cancel_after_add(event, *, timeout):
        nonlocal wait_calls
        completed = await original_wait(event, timeout=timeout)
        wait_calls += 1
        if wait_calls == 4:
            asyncio.current_task().cancel("cancel after git add")
            await asyncio.sleep(0)
        return completed

    monkeypatch.setattr(env_module, "_wait_thread_event", cancel_after_add)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(env.setup())

    workspace = env._worktree_dir
    assert env._add_owner_active is False
    assert env._worktree_registered is False
    assert env._branch_cleanup_pending is False
    assert workspace is None or not os.path.exists(workspace)
    assert not _branch_exists(repo, branch)


def test_get_diff_rejects_truncated_patch(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(repo), branch_name="truncated-diff")
    asyncio.run(env.setup())
    original_exec = env._local_env.exec_cmd

    async def truncate_diff(cmd, timeout=120.0):
        if cmd.startswith("git diff --binary"):
            return ExecResult(
                0,
                "partial patch",
                "",
                stdout_truncated=True,
                stdout_dropped_bytes=10,
            )
        return await original_exec(cmd, timeout)

    monkeypatch.setattr(env._local_env, "exec_cmd", truncate_diff)
    try:
        with pytest.raises(RuntimeError, match="diff exceeded capture limit"):
            asyncio.run(env.get_diff())
    finally:
        asyncio.run(env.cleanup())
