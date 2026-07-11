"""Tests for the workflow mode of ``run_eval_task`` (phase 4).

Three concerns:

* When ``workflow=`` is given, ``run_eval_task`` builds a ``WorkflowContext``
  whose factory creates sessions bound to the task env / budget, runs the
  workflow with the task args, and aggregates tokens (and steps) across *all*
  sessions the workflow created. Patch extraction / timeout / EvalResult shape
  stay unchanged.
* ``workflow=None`` is the unchanged single-session path (reuses the existing
  evaluator fakes).
* ``generate_review_fix`` skips its apply stage when the review verdict says no
  changes are needed.
"""

from __future__ import annotations

from evaluator_workflow_test_support import (
    CheckpointEnv,
    ExecResult,
    FakeEnv,
    WorktreeCheckpoint,
    asyncio,
    checkpoint_mod,
    is_worktree_diff_cmd,
    json,
    os,
    pytest,
    run,
    seed_checkpoint,
)


@pytest.mark.parametrize(
    "interval",
    [-1, float("nan"), float("inf"), True, "bad"],
)
def test_checkpoint_rejects_invalid_interval_at_construction(tmp_path, interval):
    with pytest.raises(ValueError, match="finite and non-negative"):
        WorktreeCheckpoint(tmp_path, interval_seconds=interval)

def test_checkpoint_abort_rejects_boolean_timeout(tmp_path):
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=0)

    with pytest.raises(ValueError, match="finite and positive"):
        run(checkpoint.abort(timeout=True))

def test_checkpoint_abort_is_bounded_when_capture_ignores_cancellation(tmp_path):
    class StubbornCaptureEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancellations = 0

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            return ExecResult(returncode=0, stdout=self.diff, stderr="")

    env = StubbornCaptureEnv()
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=0.001)

    async def scenario():
        await checkpoint.start(env)
        await asyncio.wait_for(env.started.wait(), timeout=0.5)
        capture_task = checkpoint._task
        assert capture_task is not None
        quiesced = await asyncio.wait_for(
            checkpoint.abort(timeout=0.01),
            timeout=0.5,
        )
        assert quiesced is False
        assert checkpoint._task is None
        assert env.cancellations >= 2
        assert checkpoint.pending_tasks == (capture_task,)
        env.release.set()
        await asyncio.wait_for(capture_task, timeout=0.5)
        assert checkpoint.pending_tasks == ()

    run(scenario())

def test_checkpoint_abort_rejects_invalid_timeout_without_stopping_task(tmp_path):
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)

    async def scenario():
        await checkpoint.start(FakeEnv())
        capture_task = checkpoint._task
        assert capture_task is not None
        try:
            for timeout in (float("nan"), float("inf"), 0, -1, "invalid"):
                try:
                    await checkpoint.abort(timeout=timeout)
                except ValueError as exc:
                    assert "finite and positive" in str(exc)
                else:
                    raise AssertionError(f"accepted invalid timeout: {timeout!r}")
                assert checkpoint._task is capture_task
                assert capture_task.cancelled() is False
        finally:
            assert await checkpoint.abort(timeout=0.01) is True

    run(scenario())

@pytest.mark.parametrize(
    "failure",
    [OSError("metadata disk full"), UnicodeEncodeError("utf-8", "x", 0, 1, "bad")],
)
def test_checkpoint_meta_failure_rolls_back_new_patch(monkeypatch, tmp_path, failure):
    old_patch = "diff --git a/old b/old\n+old\n"
    new_patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    checkpoint.patch_path.write_text(old_patch, encoding="utf-8")
    old_meta = {
        "schema": "opencollab.swe_worktree_checkpoint.v1",
        "status": "failed",
        "patch_sha256": checkpoint_mod._patch_sha(old_patch),
        "submission_eligible": False,
    }
    checkpoint.meta_path.write_text(json.dumps(old_meta), encoding="utf-8")
    real_atomic_write = checkpoint_mod._atomic_write

    def fail_meta(path, text, **kwargs):
        if path == checkpoint.meta_path:
            raise failure
        real_atomic_write(path, text, **kwargs)

    monkeypatch.setattr(checkpoint_mod, "_atomic_write", fail_meta)

    result = run(checkpoint.capture(CheckpointEnv(diff=new_patch), reason="periodic"))

    assert result.status == "failed"
    assert "metadata write failed" in result.error
    assert checkpoint.patch_path.read_text(encoding="utf-8") == old_patch
    assert json.loads(checkpoint.meta_path.read_text(encoding="utf-8")) == old_meta

def test_checkpoint_capture_rejects_truncated_diff_and_preserves_previous(tmp_path):
    old_patch = "diff --git a/old b/old\n+old\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, old_patch)

    class TruncatedCheckpointEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/new b/new\n+partial\n",
                    stderr="",
                    stdout_truncated=True,
                    stdout_dropped_bytes=4096,
                )
            return await super().exec_cmd(cmd, timeout)

    result = run(checkpoint.capture(TruncatedCheckpointEnv(), reason="periodic"))

    assert result.status == "failed"
    assert result.submission_eligible is True
    assert result.preserved_previous_patch is True
    assert "stdout dropped 4096 bytes" in result.error
    assert checkpoint.patch_path.read_text(encoding="utf-8") == old_patch
    meta = json.loads(checkpoint.meta_path.read_text(encoding="utf-8"))
    assert meta["submission_eligible"] is True
    restore_env = CheckpointEnv(diff_outputs=[""])
    restored = run(checkpoint.restore_latest(restore_env))
    assert restored.status == "restored"
    assert restore_env.writes[0][1] == old_patch

def test_checkpoint_restore_rejects_truncated_dirty_worktree_precheck(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class TruncatedPrecheckEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="",
                    stderr="partial warning",
                    stderr_truncated=True,
                    stderr_dropped_bytes=2048,
                )
            return await super().exec_cmd(cmd, timeout)

    env = TruncatedPrecheckEnv()
    result = run(checkpoint.restore_latest(env))

    assert result.status == "failed"
    assert result.submission_eligible is False
    assert "stderr dropped 2048 bytes" in result.error
    assert env.writes == []

def test_checkpoint_restore_rejects_oversized_patch_file(tmp_path):
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    checkpoint.patch_path.write_bytes(
        b"x" * (checkpoint_mod.MAX_CHECKPOINT_PATCH_BYTES + 1)
    )
    env = CheckpointEnv()

    result = run(checkpoint.restore_latest(env))

    assert result.status == "failed"
    assert result.submission_eligible is False
    assert "exceeds" in result.error
    assert env.writes == []

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_checkpoint_restore_rejects_fifo_without_blocking(tmp_path):
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    os.mkfifo(checkpoint.patch_path)

    result = run(asyncio.wait_for(checkpoint.restore_latest(CheckpointEnv()), 0.5))

    assert result.status == "failed"
    assert "regular file" in result.error

def test_checkpoint_capture_rejects_oversized_diff_before_write(tmp_path):
    old_patch = "diff --git a/old b/old\n+old\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, old_patch)

    class OversizedDiffEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="x" * (checkpoint_mod.MAX_CHECKPOINT_PATCH_BYTES + 1),
                    stderr="",
                )
            return await super().exec_cmd(cmd, timeout)

    result = run(checkpoint.capture(OversizedDiffEnv(), reason="periodic"))

    assert result.status == "failed"
    assert result.submission_eligible is True
    assert result.preserved_previous_patch is True
    assert "exceeds checkpoint bound" in result.error
    assert checkpoint.patch_path.read_text(encoding="utf-8") == old_patch

def test_checkpoint_capture_rejects_run_directory_swap_without_outside_write(
    tmp_path,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    old_run_dir = tmp_path / "run-old"
    outside = tmp_path / "outside"
    outside.mkdir()
    checkpoint = WorktreeCheckpoint(run_dir, interval_seconds=60)

    class SwappingEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                run_dir.rename(old_run_dir)
                run_dir.symlink_to(outside, target_is_directory=True)
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/x b/x\n+checkpoint\n",
                    stderr="",
                )
            return await super().exec_cmd(cmd, timeout)

    result = run(checkpoint.capture(SwappingEnv(), reason="periodic"))

    assert result.status == "failed"
    assert result.submission_eligible is False
    assert "parent" in result.error or "run directory" in result.error
    assert list(outside.iterdir()) == []
    assert list(old_run_dir.iterdir()) == []

def test_checkpoint_empty_capture_removes_stale_patch_and_restores_as_missing(
    tmp_path,
):
    old_patch = "diff --git a/old b/old\n+old\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, old_patch)

    captured = run(
        checkpoint.capture(
            CheckpointEnv(diff=""),
            reason="periodic",
        )
    )
    restored = run(checkpoint.restore_latest(CheckpointEnv()))

    assert captured.status == "empty"
    assert captured.patch_bytes == 0
    assert captured.patch_sha256 == ""
    assert captured.preserved_previous_patch is False
    assert captured.submission_eligible is False
    assert checkpoint.patch_path.exists() is False
    meta = json.loads(checkpoint.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "empty"
    assert meta["patch_bytes"] == 0
    assert meta["patch_sha256"] == ""
    assert restored.status == "missing"

def test_checkpoint_patch_replace_failure_rolls_back_previous_candidate(
    monkeypatch,
    tmp_path,
):
    old_patch = "diff --git a/old b/old\n+old\n"
    new_patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, old_patch)
    real_atomic_write = checkpoint_mod._atomic_write
    failed_once = False

    def replace_then_fail(path, text, **kwargs):
        nonlocal failed_once
        real_atomic_write(path, text, **kwargs)
        if path == checkpoint.patch_path and not failed_once:
            failed_once = True
            raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(checkpoint_mod, "_atomic_write", replace_then_fail)

    result = run(checkpoint.capture(CheckpointEnv(diff=new_patch), reason="periodic"))

    assert result.status == "failed"
    assert result.preserved_previous_patch is True
    assert result.submission_eligible is True
    assert "directory fsync failed after replace" in result.error
    assert checkpoint.patch_path.read_text(encoding="utf-8") == old_patch
