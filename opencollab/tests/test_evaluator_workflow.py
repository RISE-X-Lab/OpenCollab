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

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import threading
import uuid
from typing import Any

import pytest

from opencollab.adapters.env import Environment, ExecResult, LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import build_session as real_build_session
from opencollab.harness import evaluator
from opencollab.harness import swe_checkpoint as checkpoint_mod
from opencollab.harness.evaluator import EvalResult, EvalTask, run_eval_task
from opencollab.harness.swe_checkpoint import WorktreeCheckpoint
from opencollab.harness.workflows import generate_review_fix


def run(coro):
    return asyncio.run(coro)


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


def is_worktree_diff_cmd(cmd: str) -> bool:
    return "git diff --cached --binary HEAD" in cmd


class FakeEnv(Environment):
    def __init__(self, diff="diff --git a/x b/x\n+new\n"):
        self.diff = diff
        self.cleaned_up = False
        self.cmds = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


class CheckpointEnv(FakeEnv):
    def __init__(self, diff="diff --git a/x b/x\n+checkpoint\n", diff_outputs=None):
        super().__init__(diff=diff)
        self.writes: list[tuple[str, str]] = []
        self.diff_outputs = list(diff_outputs or [])

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd):
            stdout = self.diff_outputs.pop(0) if self.diff_outputs else self.diff
            return ExecResult(returncode=0, stdout=stdout, stderr="")
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def write_file(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        await self.write_file(path, content)
        return path


def seed_checkpoint(
    checkpoint: WorktreeCheckpoint,
    patch: str,
    *,
    submission_eligible: bool = True,
    status: str = "written",
) -> None:
    checkpoint.patch_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.patch_path.write_text(patch, encoding="utf-8")
    checkpoint.meta_path.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_worktree_checkpoint.v1",
                "status": status,
                "patch_bytes": len(
                    patch.encode("utf-8", errors="surrogatepass")
                ),
                "patch_sha256": checkpoint_mod._patch_sha(patch),
                "submission_eligible": submission_eligible,
                "preserved_previous_patch": status == "failed",
            }
        ),
        encoding="utf-8",
    )


class FakeSession:
    """Duck-typed workflow session that records a fixed token count."""

    def __init__(self, *, env: Any, tokens: int, reply: str = "ok") -> None:
        self.env = env
        self.used_tokens = tokens
        self.step_count = 1
        self.reply = reply
        self.messages: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.messages.append(content)

    async def run_loop(self) -> str:
        return self.reply


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
        env.release.set()
        await asyncio.wait_for(capture_task, timeout=0.5)

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


def test_checkpoint_restore_rejects_stale_metadata_for_different_patch(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    checkpoint.patch_path.write_text(patch, encoding="utf-8")
    checkpoint.meta_path.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_worktree_checkpoint.v1",
                "status": "failed",
                "patch_sha256": "0" * 64,
                "submission_eligible": False,
            }
        ),
        encoding="utf-8",
    )
    env = CheckpointEnv(diff_outputs=[""])

    result = run(checkpoint.restore_latest(env))

    assert result.status == "failed_metadata_integrity"
    assert result.submission_eligible is False
    assert "checksum" in result.error
    assert env.writes == []


def test_concurrent_checkpoint_restores_use_distinct_owned_temp_files(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)
    env = CheckpointEnv(diff_outputs=["", ""])

    async def scenario():
        return await asyncio.gather(
            checkpoint.restore_latest(env),
            checkpoint.restore_latest(env),
        )

    results = run(scenario())
    staged_paths = [path for path, _content in env.writes]

    assert [result.status for result in results] == ["restored", "restored"]
    assert len(staged_paths) == 2
    assert len(set(staged_paths)) == 2
    for path in staged_paths:
        assert any(cmd == f"rm -f -- {path}" for cmd in env.cmds)


def test_checkpoint_restore_apply_exception_removes_temp_and_reports_once(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class ApplyFailureEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                raise OSError("apply transport broke")
            return await super().exec_cmd(cmd, timeout)

    env = ApplyFailureEnv(diff_outputs=["", ""])
    result = run(checkpoint.restore_latest(env))
    staged_path = env.writes[0][0]

    assert result.status == "failed"
    assert result.error == (
        "checkpoint recovery apply failed: OSError: apply transport broke"
    )
    assert any(cmd == f"rm -f -- {staged_path}" for cmd in env.cmds)


def test_checkpoint_restore_nonzero_apply_removes_temp(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class NonzeroApplyEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                return ExecResult(returncode=1, stdout="", stderr="does not apply")
            return await super().exec_cmd(cmd, timeout)

    env = NonzeroApplyEnv(diff_outputs=["", ""])
    result = run(checkpoint.restore_latest(env))
    staged_path = env.writes[0][0]

    assert result.status == "failed"
    assert result.error == "does not apply"
    assert any(cmd == f"rm -f -- {staged_path}" for cmd in env.cmds)


def test_checkpoint_failed_restore_proof_has_total_deadline(
    tmp_path,
    monkeypatch,
):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)
    monkeypatch.setattr(checkpoint_mod, "MAX_FAILED_RESTORE_PROOF_SECONDS", 0.02)

    class HangingProofEnv(CheckpointEnv):
        def __init__(self):
            super().__init__()
            self.diff_calls = 0

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                self.diff_calls += 1
                if self.diff_calls == 1:
                    return ExecResult(returncode=0, stdout="", stderr="")
                await asyncio.Event().wait()
            if cmd.startswith("git apply"):
                return ExecResult(returncode=1, stdout="", stderr="does not apply")
            return await super().exec_cmd(cmd, timeout)

    async def scenario():
        env = HangingProofEnv()
        result = await asyncio.wait_for(checkpoint.restore_latest(env), timeout=0.5)
        await asyncio.sleep(0)
        quiesced = await checkpoint.abort(timeout=0.05)
        return env, result, quiesced

    env, result, quiesced = run(scenario())

    assert result.status == "failed"
    assert result.worktree_integrity_proven is False
    assert "proof exceeded its deadline" in result.error
    assert env._aborted is True
    assert quiesced is True


def test_checkpoint_restore_cancelled_apply_removes_temp_before_propagating(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class BlockingApplyEnv(CheckpointEnv):
        def __init__(self):
            super().__init__(diff_outputs=[""])
            self.apply_started = asyncio.Event()

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                self.apply_started.set()
                await asyncio.Event().wait()
            return await super().exec_cmd(cmd, timeout)

    async def scenario():
        env = BlockingApplyEnv()
        task = asyncio.create_task(checkpoint.restore_latest(env))
        await env.apply_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return env

    env = run(scenario())
    staged_path = env.writes[0][0]

    assert any(cmd == f"rm -f -- {staged_path}" for cmd in env.cmds)


def test_checkpoint_restore_temp_cleanup_failure_is_ineligible(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class RemovalFailureEnv(CheckpointEnv):
        async def remove_file(self, path: str) -> None:
            raise OSError("cannot unlink recovery patch")

    result = run(
        checkpoint.restore_latest(RemovalFailureEnv(diff_outputs=[""]))
    )

    assert result.status == "failed"
    assert result.submission_eligible is False
    assert result.error == (
        "checkpoint recovery temporary-file cleanup failed: "
        "OSError: cannot unlink recovery patch"
    )


def test_checkpoint_restore_nonzero_apply_detects_partial_worktree_mutation(tmp_path):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class PartialApplyEnv(CheckpointEnv):
        def __init__(self):
            super().__init__(diff_outputs=[""])
            self.partial = False

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                self.partial = True
                return ExecResult(returncode=1, stdout="", stderr="partial failure")
            if is_worktree_diff_cmd(cmd) and self.partial:
                self.cmds.append(cmd)
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/partial b/partial\n+leak\n",
                    stderr="",
                )
            return await super().exec_cmd(cmd, timeout)

    result = run(checkpoint.restore_latest(PartialApplyEnv()))

    assert result.status == "failed"
    assert result.worktree_integrity_proven is False
    assert "left the worktree dirty" in result.error


def test_checkpoint_restore_cancelled_partial_apply_marks_cancellation_unproven(
    tmp_path,
):
    patch = "diff --git a/new b/new\n+new\n"
    checkpoint = WorktreeCheckpoint(tmp_path, interval_seconds=60)
    seed_checkpoint(checkpoint, patch)

    class CancelledPartialEnv(CheckpointEnv):
        def __init__(self):
            super().__init__(diff_outputs=[""])
            self.partial = False

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                self.partial = True
                raise asyncio.CancelledError("cancelled after partial write")
            if is_worktree_diff_cmd(cmd) and self.partial:
                self.cmds.append(cmd)
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/partial b/partial\n+leak\n",
                    stderr="",
                )
            return await super().exec_cmd(cmd, timeout)

    with pytest.raises(asyncio.CancelledError) as raised:
        run(checkpoint.restore_latest(CancelledPartialEnv()))

    assert raised.value.checkpoint_restore_integrity_proven is False
    assert any("left the worktree dirty" in note for note in raised.value.__notes__)


def test_evaluator_blocks_workflow_and_patch_after_partial_checkpoint_restore(
    tmp_path,
):
    run_dir = tmp_path / "trajectories" / "partial-restore"
    checkpoint_patch = "diff --git a/new b/new\n+new\n"
    seed_checkpoint(WorktreeCheckpoint(run_dir), checkpoint_patch)
    workflow_ran = False

    class PartialRestoreEnv(CheckpointEnv):
        def __init__(self):
            super().__init__(diff_outputs=[""])
            self.partial = False

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply"):
                self.cmds.append(cmd)
                self.partial = True
                return ExecResult(returncode=1, stdout="", stderr="partial failure")
            if is_worktree_diff_cmd(cmd) and self.partial:
                self.cmds.append(cmd)
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/partial b/partial\n+leak\n",
                    stderr="",
                )
            return await super().exec_cmd(cmd, timeout)

    env = PartialRestoreEnv()

    async def env_factory(task):
        return env

    async def workflow(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="partial-restore", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    assert workflow_ran is False
    assert result.checkpoint_restore_integrity_proven is False
    assert result.checkpoint_result["restore"]["worktree_integrity_proven"] is False
    assert result.checkpoint_result["final"]["status"] == (
        "skipped_checkpoint_restore_integrity_failure"
    )
    assert result.patch == ""
    assert result.patch_produced is False
    assert result.submission_eligible is False


# --------------------------------------------------------------------------- #
# workflow mode: invocation + aggregation
# --------------------------------------------------------------------------- #


def test_workflow_mode_invoked_with_task_args(tmp_path):
    seen: dict[str, Any] = {}
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        seen["args"] = args
        seen["ctx"] = ctx
        return "done"

    result = run(
        run_eval_task(
            EvalTask(task_id="t1", description="fix the bug"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert isinstance(result, EvalResult)
    assert result.task_id == "t1"
    assert seen["args"]["task_id"] == "t1"
    assert seen["args"]["description"] == "fix the bug"
    # Patch extraction is unchanged: the env diff still becomes the patch.
    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error is None
    assert env.cleaned_up is True


def test_workflow_manifest_failure_preserves_patch_and_metrics(monkeypatch, tmp_path):
    import opencollab.harness.evaluator as evaluator_mod

    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        ctx._sessions.append(FakeSession(env=env, tokens=7))
        return {"status": "done"}

    def fail_manifest(*args, **kwargs):
        raise OSError("manifest disk failure")

    monkeypatch.setattr(evaluator_mod.SessionStore, "save_manifest", fail_manifest)

    result = run(
        run_eval_task(
            EvalTask(task_id="manifest-failure", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert env.cleaned_up is True
    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.tokens_used == 7
    assert result.error == "workflow manifest failed: OSError: manifest disk failure"


def test_eval_workflow_slow_manifest_is_bounded_and_defers_resources(
    monkeypatch,
    tmp_path,
):
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    tracers: list[Any] = []

    class ResourceEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.cleanup_event = asyncio.Event()

        async def cleanup(self):
            order.append("environment-cleanup")
            await super().cleanup()
            self.cleanup_event.set()

    class RecordingTracer:
        write_error = None

        def __init__(self, *, run_id, output_dir, filename=None):
            self.path = os.path.join(output_dir, filename or f"{run_id}.jsonl")
            self.closed = False
            self.closed_event = asyncio.Event()
            tracers.append(self)

        def log_step(self, *args, **kwargs):
            return None

        def close(self):
            self.closed = True
            order.append("tracer-close")
            self.closed_event.set()

    original_manifest = evaluator._write_eval_workflow_manifest

    def blocking_manifest(*args, **kwargs):
        order.append("manifest-start")
        started.set()
        assert release.wait(timeout=2.0)
        original_manifest(*args, **kwargs)
        order.append("manifest-end")

    monkeypatch.setattr(evaluator, "Tracer", RecordingTracer)
    monkeypatch.setattr(
        evaluator,
        "_write_eval_workflow_manifest",
        blocking_manifest,
    )

    async def scenario():
        env = ResourceEnv()

        async def env_factory(task):
            return env

        async def wf(ctx, args):
            return "done"

        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="slow-manifest", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                cancellation_cleanup_timeout=0.01,
            )
        )
        assert await asyncio.to_thread(started.wait, 0.5)
        await asyncio.wait_for(asyncio.sleep(0.005), timeout=0.05)
        result = await asyncio.wait_for(evaluation, timeout=0.3)
        assert tracers[0].closed is False
        assert env.cleaned_up is True
        assert evaluator._EVAL_MANIFEST_OWNER_TASKS
        release.set()
        await asyncio.wait_for(tracers[0].closed_event.wait(), timeout=0.5)
        assert env.cleanup_event.is_set()
        return result

    result = run(scenario())

    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert "workflow manifest timed out" in result.error
    assert order.index("environment-cleanup") < order.index("manifest-end")
    assert order.index("manifest-end") < order.index("tracer-close")


def test_deferred_eval_tracer_close_survives_owner_cancellation():
    async def scenario():
        release = asyncio.Event()

        async def dependency():
            await release.wait()

        dependency_task = asyncio.create_task(dependency())

        class RecordingTracer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        tracer = RecordingTracer()
        owner = asyncio.create_task(
            evaluator._cleanup_eval_resources_after_tasks(
                (dependency_task,),
                tracer=tracer,
                timeout=0.2,
            )
        )
        await asyncio.sleep(0)
        owner.cancel("loop shutdown")
        owner.cancel("loop shutdown repeated")
        release.set()
        await asyncio.wait_for(owner, timeout=0.5)
        return tracer

    tracer = run(scenario())
    assert tracer.closed is True


def test_deferred_eval_tracer_close_has_final_deadline():
    async def scenario():
        dependency_task = asyncio.create_task(asyncio.Event().wait())

        class RecordingTracer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        tracer = RecordingTracer()
        failures_before = len(evaluator._LATE_EVAL_RESOURCE_FAILURES)
        await asyncio.wait_for(
            evaluator._cleanup_eval_resources_after_tasks(
                (dependency_task,),
                tracer=tracer,
                timeout=0.01,
            ),
            timeout=0.2,
        )
        dependency_task.cancel()
        await asyncio.gather(dependency_task, return_exceptions=True)
        return tracer, failures_before

    tracer, failures_before = run(scenario())
    assert tracer.closed is True
    assert len(evaluator._LATE_EVAL_RESOURCE_FAILURES) == failures_before + 1
    assert isinstance(evaluator._LATE_EVAL_RESOURCE_FAILURES[-1], TimeoutError)


def test_eval_workflow_cancel_during_manifest_preserves_cancel_and_adds_note(
    monkeypatch,
    tmp_path,
):
    started = threading.Event()
    release = threading.Event()
    env = FakeEnv()

    def failing_manifest(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        raise OSError("eval manifest disk failed")

    monkeypatch.setattr(
        evaluator,
        "_write_eval_workflow_manifest",
        failing_manifest,
    )

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancel-manifest", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                cancellation_cleanup_timeout=0.2,
            )
        )
        assert await asyncio.to_thread(started.wait, 0.5)
        evaluation.cancel("primary cancellation")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(evaluation, timeout=0.5)
        return caught.value

    cancellation = run(scenario())

    assert cancellation.args == ("primary cancellation",)
    assert any(
        "workflow manifest failed" in note
        and "eval manifest disk failed" in note
        for note in cancellation.__notes__
    )
    assert env.cleaned_up is True


def test_workflow_mode_aggregates_tokens_across_sessions(tmp_path):
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        # Two agent calls -> two sessions; tokens must sum across both.
        await ctx.agent("first")
        await ctx.agent("second")
        return "done"

    # Each fake session reports 7 tokens; the factory builds real sessions, so
    # we patch the factory's session builder to return token-bearing fakes.
    import opencollab.harness.evaluator as evaluator_mod

    original = evaluator_mod._build_eval_session_factory

    def patched_factory(*args, **kwargs):
        factory = original(*args, **kwargs)

        def build(
            *, prompt, budget, tools=None, isolation=False, label=None,
            tool_choice=None, thinking=None,
        ):
            return FakeSession(env=env, tokens=7)

        factory.build_workflow_session = build  # type: ignore[attr-defined]
        return factory

    evaluator_mod._build_eval_session_factory = patched_factory
    try:
        result = run(
            run_eval_task(
                EvalTask(task_id="t2", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )
    finally:
        evaluator_mod._build_eval_session_factory = original

    assert result.tokens_used == 14
    assert result.steps == 2


# --------------------------------------------------------------------------- #
# workflow mode: per-task run folder layout (per-role + orchestration + manifest)
# --------------------------------------------------------------------------- #


def test_workflow_mode_writes_per_task_run_folder(tmp_path):
    """Workflow mode lands a per-task folder: orchestration.jsonl + workflow.json.

    Mirrors a team / CLI workflow run: the scheduling signals go to one
    ``orchestration.jsonl`` and a ``workflow.json`` manifest ties the run folder
    together. The legacy flat ``trajectories/<task_id>.jsonl`` must NOT appear.
    """
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        await ctx.phase("implement")
        await ctx.agent("do the work")  # one session via the token-bearing factory
        return "done"

    with _token_bearing_factory(env):
        result = run(
            run_eval_task(
                EvalTask(task_id="wf1", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )

    run_dir = tmp_path / "trajectories" / "wf1"
    orch = run_dir / "orchestration.jsonl"
    manifest_path = run_dir / "workflow.json"
    assert orch.exists()
    # The flat single-file trajectory is gone for workflow mode.
    assert not (tmp_path / "trajectories" / "wf1.jsonl").exists()
    # EvalResult.trajectory_path points at the orchestration file in the folder.
    assert result.trajectory_path == str(orch)

    types = [json.loads(line)["type"] for line in orch.read_text().splitlines() if line.strip()]
    assert "workflow_phase" in types

    manifest = json.loads(manifest_path.read_text())
    assert manifest["workflow"] == "wf"
    assert manifest["task_id"] == "wf1"
    assert manifest["sessions"] == 1


def test_eval_workflow_final_snapshot_captures_post_step_mutation(
    monkeypatch,
    tmp_path,
):
    env = FakeEnv()

    class ReplyLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            return LLMResponse(
                content="finished",
                usage=Usage(input_tokens=5, output_tokens=2),
                finish_reason="stop",
            )

    llm = ReplyLLM()

    def build_real(**kwargs):
        return real_build_session(llm=llm, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", build_real)
    holder: dict[str, Any] = {}

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        assert await ctx.agent("one turn", label="worker") == "finished"
        session = ctx.sessions[0]
        session.state.append_message(
            {"role": "user", "content": "late evaluator mutation"}
        )
        holder["session"] = session
        return "done"

    result = run(
        run_eval_task(
            EvalTask(task_id="final-snapshot", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.error is None
    path = tmp_path / "trajectories" / "final-snapshot" / "000_worker.json"
    snapshot = SessionStore().load_snapshot(str(path), "system")
    assert snapshot["messages"][-1]["content"] == "late evaluator mutation"
    assert snapshot["session_state"]["phase"] == holder["session"].state.phase.value


def test_eval_workflow_final_snapshot_captures_session_exception(
    monkeypatch,
    tmp_path,
):
    env = FakeEnv()

    class FailingLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            raise RuntimeError("eval provider exploded")

    def build_real(**kwargs):
        return real_build_session(llm=FailingLLM(), **kwargs)

    monkeypatch.setattr(evaluator, "build_session", build_real)
    holder: dict[str, Any] = {}

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        assert await ctx.agent("fail", label="broken") is None
        holder["session"] = ctx.sessions[0]
        return "continued"

    result = run(
        run_eval_task(
            EvalTask(task_id="exception-snapshot", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.error is None
    session = holder["session"]
    path = tmp_path / "trajectories" / "exception-snapshot" / "000_broken.json"
    snapshot = SessionStore().load_snapshot(str(path), "system")
    assert snapshot["session_state"]["phase"] == session.state.phase.value == "error"
    assert snapshot["session_state"]["terminal_reason"] == session.state.terminal_reason
    assert "eval provider exploded" in snapshot["session_state"]["terminal_reason"]
    assert snapshot["session_state"]["pending_events"] == []


def test_workflow_checkpoint_writes_bounded_loss_patch(tmp_path):
    env = CheckpointEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="ckpt", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    run_dir = tmp_path / "trajectories" / "ckpt"
    patch_path = run_dir / "checkpoint.worktree.patch"
    meta = json.loads((run_dir / "checkpoint.worktree.json").read_text(encoding="utf-8"))
    assert patch_path.read_text(encoding="utf-8") == env.diff
    assert meta["status"] == "written"
    assert meta["reason"] == "final"
    assert meta["loss_bound_seconds"] == 300
    assert meta["submission_eligible"] is True
    assert result.checkpoint_result["final"]["status"] == "written"


def test_workflow_checkpoint_restore_applies_before_test_injection(tmp_path):
    env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "restore"
    checkpoint_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n"
    seed_checkpoint(WorktreeCheckpoint(run_dir), checkpoint_patch)

    result = run(
        run_eval_task(
            EvalTask(
                task_id="restore",
                description="x",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
                        "--- a/tests/test_x.py\n"
                        "+++ b/tests/test_x.py\n"
                        "@@ -0,0 +1 @@\n"
                        "+test\n"
                    ),
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    recovery_path, recovery_content = env.writes[0]
    assert recovery_path.startswith("/tmp/opencollab-checkpoint-recovery-")
    assert recovery_path.endswith(".patch")
    assert recovery_content == checkpoint_patch
    restore_index = next(i for i, cmd in enumerate(env.cmds) if cmd.startswith("git apply"))
    test_injection_index = next(
        i
        for i, cmd in enumerate(env.cmds)
        if "opencollab-test-patch-" in cmd
    )
    assert restore_index < test_injection_index
    assert result.checkpoint_result["restore"]["status"] == "restored"


def test_workflow_checkpoint_restore_skips_dirty_worktree(tmp_path):
    env = CheckpointEnv(diff_outputs=["diff --git a/dirty b/dirty\n+dirty\n"])

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "dirty"
    seed_checkpoint(
        WorktreeCheckpoint(run_dir),
        "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n",
    )

    result = run(
        run_eval_task(
            EvalTask(task_id="dirty", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    assert result.checkpoint_result["restore"]["status"] == "skipped_dirty_worktree"
    assert not any(cmd.startswith("git apply") for cmd in env.cmds)


def test_workflow_checkpoint_restore_rejects_corrupt_metadata(tmp_path):
    env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "corrupt"
    run_dir.mkdir(parents=True)
    checkpoint_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n"
    (run_dir / "checkpoint.worktree.patch").write_text(checkpoint_patch, encoding="utf-8")
    (run_dir / "checkpoint.worktree.json").write_text("{bad json", encoding="utf-8")

    result = run(
        run_eval_task(
            EvalTask(task_id="corrupt", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    assert result.checkpoint_result["restore"]["status"] == (
        "failed_metadata_integrity"
    )
    assert result.checkpoint_result["restore"]["submission_eligible"] is False
    assert env.writes == []


def test_workflow_checkpoint_restore_path_is_private_per_run_dir(tmp_path):
    first_env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])
    second_env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])

    async def wf(ctx, args):
        return {"status": "done"}

    for task_id, env in (("first", first_env), ("second", second_env)):
        run_dir = tmp_path / "trajectories" / task_id
        seed_checkpoint(
            WorktreeCheckpoint(run_dir),
            "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n",
        )

        async def env_factory(task, env=env):
            return env

        run(
            run_eval_task(
                EvalTask(task_id=task_id, description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                checkpoint_interval_seconds=300,
                resume_from_checkpoint=True,
            )
        )

    assert first_env.writes[0][0] != second_env.writes[0][0]
    assert first_env.writes[0][0].startswith("/tmp/opencollab-checkpoint-recovery-")
    assert second_env.writes[0][0].startswith("/tmp/opencollab-checkpoint-recovery-")


def test_workflow_checkpoint_restore_respects_ineligible_metadata(tmp_path):
    env = CheckpointEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "ineligible"
    seed_checkpoint(
        WorktreeCheckpoint(run_dir),
        "diff --git a/pkg/a.py b/pkg/a.py\n+old\n",
        submission_eligible=False,
        status="failed",
    )

    result = run(
        run_eval_task(
            EvalTask(task_id="ineligible", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    assert result.checkpoint_result["restore"]["status"] == "skipped_not_submission_eligible"
    assert not env.writes


def test_workflow_checkpoint_excludes_injected_test_paths(tmp_path):
    env = CheckpointEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run(
        run_eval_task(
            EvalTask(
                task_id="exclude",
                description="x",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
                        "--- a/tests/test_x.py\n"
                        "+++ b/tests/test_x.py\n"
                        "@@ -0,0 +1 @@\n"
                        "+test\n"
                    ),
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    checkpoint_cmds = [cmd for cmd in env.cmds if "git diff --cached --binary HEAD" in cmd]
    assert checkpoint_cmds
    assert (
        "git --literal-pathspecs reset -q HEAD -- tests/test_x.py"
        in checkpoint_cmds[-1]
    )


def test_workflow_checkpoint_excludes_own_artifacts_inside_workspace(tmp_path):
    env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])
    env.workspace = str(tmp_path)
    env.local_filesystem = True

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    output_dir = tmp_path / "eval_results"
    run_dir = output_dir / "trajectories" / "inside"
    checkpoint_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n"
    seed_checkpoint(WorktreeCheckpoint(run_dir), checkpoint_patch)

    result = run(
        run_eval_task(
            EvalTask(task_id="inside", description="x"),
            output_dir=str(output_dir),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
            resume_from_checkpoint=True,
        )
    )

    checkpoint_cmds = [cmd for cmd in env.cmds if "git diff --cached --binary HEAD" in cmd]
    assert checkpoint_cmds
    assert result.checkpoint_result["restore"]["status"] == "restored"
    assert (
        "git --literal-pathspecs reset -q HEAD -- "
        "eval_results/trajectories/inside/checkpoint.worktree.patch"
        in checkpoint_cmds[0]
    )
    assert (
        "git --literal-pathspecs reset -q HEAD -- "
        "eval_results/trajectories/inside/checkpoint.worktree.json"
        in checkpoint_cmds[0]
    )


def test_evaluator_artifacts_inside_repo_never_enter_patch_or_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "results.jsonl").write_text('{"old": true}\n', encoding="utf-8")
    stale_temp = repo / ".results.jsonl.crash.tmp"
    stale_temp.write_text('{"secret": true}\n', encoding="utf-8")
    env = LocalEnvironment(str(repo))

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="artifact-isolation", description="fix"),
            output_dir=str(repo),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    assert result.patch == ""
    assert result.patch_produced is False
    assert result.patch_extraction_succeeded is True
    assert result.submission_eligible is True
    assert result.checkpoint_result["final"]["submission_eligible"] is False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "results.jsonl" in status
    assert ".results.jsonl.crash.tmp" in status
    assert "trajectories/" in status


def test_legacy_result_temp_scan_overflow_stops_workflow_and_blanks_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    for index in range(evaluator.MAX_LEGACY_RESULT_TEMP_ARTIFACTS + 1):
        (repo / f".results.jsonl.{index}.tmp").write_text(
            f'{{"secret": {index}}}\n',
            encoding="utf-8",
        )
    workflow_ran = False

    async def env_factory(task):
        return LocalEnvironment(str(repo))

    async def wf(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="legacy-overflow", description="fix"),
            output_dir=str(repo),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert workflow_ran is False
    assert result.harness_artifact_exclusion_proven is False
    assert result.submission_eligible is False
    assert result.patch == ""
    assert result.patch_produced is False
    assert "legacy result temp artifact scan exceeded" in result.error


def test_mapped_artifact_bound_failure_stops_workflow_and_blanks_patch(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    artifact = repo / "tasks.jsonl"
    artifact.write_text('{"secret": true}\n', encoding="utf-8")
    monkeypatch.setattr(evaluator, "MAX_MAPPED_HARNESS_ARTIFACT_PATHS", 0)
    workflow_ran = False

    async def env_factory(task):
        return LocalEnvironment(str(repo))

    async def wf(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(
                task_id="mapped-overflow",
                description="fix",
                harness_artifact_paths=(str(artifact),),
            ),
            output_dir=str(tmp_path / "outside-output"),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert workflow_ran is False
    assert result.harness_artifact_exclusion_proven is False
    assert result.submission_eligible is False
    assert result.patch == ""
    assert "mapped artifact path count" in result.error


def test_checkpoint_never_maps_host_artifacts_into_non_local_workspace():
    class NonLocalEnv(Environment):
        workspace = "/testbed"
        local_filesystem = False

    checkpoint = WorktreeCheckpoint(
        "/testbed/eval_results/trajectories/container-task"
    )

    assert checkpoint._artifact_exclude_paths(NonLocalEnv()) == ()


def test_worktree_diff_uses_only_alternate_index_while_real_lock_is_held(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "source.py").write_text("new\n", encoding="utf-8")
    (repo / "harness.tmp").write_text("secret\n", encoding="utf-8")
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    index = repo / git_dir / "index"
    index_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    lock = repo / git_dir / "index.lock"
    lock.write_text("held\n", encoding="utf-8")

    try:
        result = subprocess.run(
            ["bash", "-lc", checkpoint_mod.worktree_diff_command(["harness.tmp"])],
            cwd=repo,
            text=True,
            capture_output=True,
        )
    finally:
        lock.unlink()

    assert result.returncode == 0, result.stderr
    assert "source.py" in result.stdout
    assert "harness.tmp" not in result.stdout
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_hash
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert staged == ""


def test_worktree_diff_exclusion_reset_failure_cannot_fall_through_to_diff():
    command = checkpoint_mod.worktree_diff_command(["harness.tmp"])

    assert "|| true" not in command
    assert (
        "git --literal-pathspecs reset -q HEAD -- harness.tmp && "
        'GIT_INDEX_FILE="$idx" git diff --cached --binary HEAD'
        in command
    )


def test_bind_mounted_docker_artifacts_never_enter_patch_or_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )

    class BindMountedEnv(Environment):
        workspace = "/workspace"
        local_filesystem = False

        def __init__(self):
            self.host_workspace = str(repo)
            self.host = LocalEnvironment(str(repo))

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return await self.host.exec_cmd(cmd, timeout)

        async def read_file(self, path: str) -> str:
            return await self.host.read_file(path)

        async def write_file(self, path: str, content: str) -> None:
            await self.host.write_file(path, content)

    env = BindMountedEnv()
    output_dir = repo / "eval_results"

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="bind-artifact-isolation", description="fix"),
            output_dir=str(output_dir),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    assert result.patch == ""
    assert result.patch_extraction_succeeded is True
    assert result.submission_eligible is True
    assert result.checkpoint_result["final"]["submission_eligible"] is False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "eval_results/" in status


def test_workflow_checkpoint_capture_failure_does_not_reendorse_orphan_patch(tmp_path):
    class FailingCheckpointEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            self.cmds.append(cmd)
            if "git diff --cached --binary HEAD" in cmd:
                return ExecResult(returncode=1, stdout="", stderr="diff failed")
            return ExecResult(returncode=0, stdout="", stderr="")

    env = FailingCheckpointEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "preserve"
    run_dir.mkdir(parents=True)
    old_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+old\n"
    (run_dir / "checkpoint.worktree.patch").write_text(old_patch, encoding="utf-8")

    result = run(
        run_eval_task(
            EvalTask(task_id="preserve", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    meta = json.loads((run_dir / "checkpoint.worktree.json").read_text(encoding="utf-8"))
    assert (run_dir / "checkpoint.worktree.patch").read_text(encoding="utf-8") == old_patch
    assert meta["status"] == "failed"
    assert meta["preserved_previous_patch"] is True
    assert meta["submission_eligible"] is False
    assert result.checkpoint_result["final"]["submission_eligible"] is False
    assert result.patch_produced is False


def test_eval_factory_threads_per_role_transcript_path(monkeypatch, tmp_path):
    """The eval factory autosaves each session per role: ``<seq>_<role>.json``."""
    import opencollab.harness.evaluator as evaluator_mod

    calls: list[dict[str, Any]] = []

    def fake_build_session(*, agent, **kwargs):
        calls.append(kwargs)
        return FakeSession(env=FakeEnv(), tokens=0)

    monkeypatch.setattr(evaluator_mod, "build_session", fake_build_session)

    save_dir = str(tmp_path / "trajectories" / "t")
    factory = evaluator_mod._build_eval_session_factory(
        env=FakeEnv(),
        tracer=None,
        prompt="sys",
        model="m",
        provider="p",
        api_key=None,
        base_url=None,
        max_steps=10,
        default_toolset=[],
        save_dir=save_dir,
    )

    factory.build_workflow_session(prompt="a", budget=100, label="analyst")
    factory.build_workflow_session(prompt="b", budget=100, label="coder:s1r2")

    assert [c["auto_save_path"] for c in calls] == [
        os.path.join(save_dir, "000_analyst.json"),
        os.path.join(save_dir, "001_coder-s1r2.json"),
    ]


def test_single_session_mode_keeps_flat_trajectory(monkeypatch, tmp_path):
    """workflow=None is unchanged: one flat ``trajectories/<task_id>.jsonl``."""
    from opencollab.adapters.llm import LLMResponse, Usage
    from opencollab.bootstrap import container

    class FakeLLMClient:
        def __init__(self, *a, **k):
            pass

        async def complete(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="done",
                tool_calls=[],
                usage=Usage(input_tokens=3, output_tokens=2),
                finish_reason="stop",
            )

    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="flat1", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    flat = tmp_path / "trajectories" / "flat1.jsonl"
    assert flat.exists()
    assert result.trajectory_path == str(flat)
    # No per-task folder is created in single-session mode.
    assert not (tmp_path / "trajectories" / "flat1").is_dir()


# --------------------------------------------------------------------------- #
# workflow mode: abnormal endings must NOT zero metrics or drop the patch
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _token_bearing_factory(env: Any, tokens: int = 7):
    """Patch the eval session factory so workflow agents report fixed tokens/steps.

    Mirrors the inline patch in ``test_workflow_mode_aggregates_tokens_across_sessions``
    so abnormal-exit tests can assert metrics survived (each agent -> 1 session,
    ``tokens`` tokens, 1 step).
    """
    import opencollab.harness.evaluator as evaluator_mod

    original = evaluator_mod._build_eval_session_factory

    def patched_factory(*args, **kwargs):
        factory = original(*args, **kwargs)

        def build(
            *, prompt, budget, tools=None, isolation=False, label=None,
            tool_choice=None, thinking=None,
        ):
            return FakeSession(env=env, tokens=tokens)

        factory.build_workflow_session = build  # type: ignore[attr-defined]
        return factory

    evaluator_mod._build_eval_session_factory = patched_factory
    try:
        yield
    finally:
        evaluator_mod._build_eval_session_factory = original


def test_workflow_budget_exceeded_preserves_metrics_and_patch(tmp_path):
    """A budget-floor stop still reports real metrics AND submits the on-disk patch.

    Regression: when the workflow raised ``WorkflowBudgetExceeded`` the caller's
    ``workflow_ctx`` stayed None, zeroing tokens/steps; and ``patch_produced`` was
    gated on ``error is None``. Now ``_run_workflow_mode`` returns the ctx (whose
    sessions hold the metrics) and the on-disk diff is a real patch regardless of
    how the run ended. Budget-floor exhaustion is BY DESIGN -> no error.
    """
    from opencollab.application.workflow import WorkflowBudgetExceeded

    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        await ctx.agent("did some work")  # one session: 7 tokens, 1 step
        raise WorkflowBudgetExceeded("workflow budget exhausted: spent 9 of 5")

    with _token_bearing_factory(env):
        result = run(
            run_eval_task(
                EvalTask(task_id="b1", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )

    assert result.tokens_used == 7  # not zeroed
    assert result.steps == 1  # not zeroed
    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error is None  # budget floor is controlled, not a failure
    assert result.submission_eligible is True


def test_workflow_provider_timeout_records_transport_error_and_keeps_metrics(tmp_path):
    """A provider timeout keeps the partial patch + metrics and its real cause.

    A provider ``asyncio.TimeoutError`` may occur inside the workflow before the
    caller deadline. Such an ending still surfaces metrics and the on-disk patch, with the cause
    recorded in ``error`` for observability (``patch_produced`` stays honest off
    the real diff, no longer gated on ``error is None``).
    """
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        await ctx.agent("did some work")  # one session: 7 tokens, 1 step
        raise asyncio.TimeoutError()

    with _token_bearing_factory(env):
        result = run(
            run_eval_task(
                EvalTask(task_id="b2", description="x", timeout=123),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )

    assert result.tokens_used == 7  # not zeroed
    assert result.steps == 1  # not zeroed
    assert result.patch == env.diff
    assert result.patch_produced is True  # real patch regardless of the error
    assert result.error is not None and result.error.startswith("TimeoutError:")
    assert result.submission_eligible is True


def test_workflow_error_is_preserved_when_tracer_close_also_fails(monkeypatch, tmp_path):
    import opencollab.harness.evaluator as evaluator_mod

    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        raise asyncio.TimeoutError("provider timeout")

    def fail_close(self):
        if not getattr(self, "_test_close_failed", False):
            self._test_close_failed = True
            raise OSError("trace disk failure")

    monkeypatch.setattr(evaluator_mod.Tracer, "close", fail_close)

    result = run(
        run_eval_task(
            EvalTask(task_id="combined-error", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.patch == env.diff
    assert result.error == (
        "TimeoutError: provider timeout; "
        "tracer close failed: OSError: trace disk failure"
    )


def test_workflow_caller_deadline_is_reported_as_task_timeout(tmp_path):
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        await asyncio.Event().wait()

    result = run(
        run_eval_task(
            EvalTask(task_id="deadline", description="x", timeout=0.01),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.error == "Task timed out after 0.01s"


def test_workflow_deadline_waits_for_cancel_cleanup_before_patch_extraction(tmp_path):
    env = FakeEnv(diff="diff --git a/x b/x\n+early\n")
    cancel_seen = asyncio.Event()
    release_cancel = asyncio.Event()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release_cancel.wait()
            ctx._sessions.append(FakeSession(env=env, tokens=19))
            env.diff = "diff --git a/x b/x\n+late-workflow-write\n"
            raise

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="deadline-cleanup", description="x", timeout=0.01),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )
        await cancel_seen.wait()
        await asyncio.sleep(0)
        assert eval_task.done() is False
        assert not any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
        release_cancel.set()
        return await eval_task

    result = run(scenario())

    assert result.error == "Task timed out after 0.01s"
    assert result.tokens_used == 19
    assert "late-workflow-write" in result.patch
    assert result.submission_eligible is True


def test_environment_setup_late_result_is_adopted_and_cleaned(tmp_path):
    env = FakeEnv(diff="diff --git a/preexisting b/preexisting\n+dirty\n")

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            return env

    result = run(
        run_eval_task(
            EvalTask(task_id="late-env", description="x", timeout=0.02),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.1,
        )
    )

    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.execution_quiesced is True
    assert result.task_stage_integrity_proven is False
    assert result.submission_eligible is False


def test_environment_setup_late_result_cleanup_failure_blocks_submission(tmp_path):
    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self):
            raise OSError("late cleanup failed")

    env = CleanupFailureEnv()

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return env

    result = run(
        run_eval_task(
            EvalTask(task_id="late-env-cleanup-failure", description="x", timeout=0.02),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.05,
        )
    )

    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert "environment cleanup failed" in result.error


def test_late_environment_cancelled_teardown_is_bounded_and_visible(tmp_path):
    class CancelledTeardownEnv(FakeEnv):
        async def abort(self):
            raise asyncio.CancelledError("abort cancelled forever")

        async def cleanup(self):
            raise asyncio.CancelledError("cleanup cancelled forever")

    env = CancelledTeardownEnv()

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return env

    async def scenario():
        return await asyncio.wait_for(
            run_eval_task(
                EvalTask(
                    task_id="cancelled-late-teardown",
                    description="x",
                    timeout=0.01,
                ),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )

    result = run(scenario())

    assert env._aborted is True
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert "environment abort failed: CancelledError" in result.error
    assert "environment cleanup failed: CancelledError" in result.error


def test_caller_cancel_adopts_late_environment_before_propagating(tmp_path):
    env = FakeEnv()
    setup_started = asyncio.Event()

    async def env_factory(task):
        setup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            return env

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancel-late-env", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.1,
            )
        )
        await setup_started.wait()
        evaluation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await evaluation

    run(scenario())

    assert env.cleaned_up is True


def test_caller_cancel_keeps_environment_cleanup_failure_in_note(tmp_path):
    started = asyncio.Event()

    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self):
            raise OSError("environment cleanup exploded")

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancel-cleanup-failure", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                cancellation_cleanup_timeout=0.05,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        evaluation.cancel("primary cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(evaluation, timeout=0.5)
        return caught.value

    cancellation = run(scenario())

    assert cancellation.args == ("primary cancellation",)
    assert any(
        "environment cleanup failed" in note
        and "environment cleanup exploded" in note
        for note in cancellation.__notes__
    )


def test_environment_returning_after_cleanup_bound_is_revoked_and_cleaned(tmp_path):
    release_setup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class LateEnv(FakeEnv):
        async def cleanup(self):
            await super().cleanup()
            cleanup_finished.set()

    env = LateEnv()

    async def env_factory(task):
        while not release_setup.is_set():
            try:
                await release_setup.wait()
            except asyncio.CancelledError:
                continue
        return env

    async def scenario():
        result = await run_eval_task(
            EvalTask(task_id="very-late-env", description="x", timeout=0.02),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            cancellation_cleanup_timeout=0.01,
        )
        assert result.execution_quiesced is False
        assert result.submission_eligible is False
        release_setup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert result.patch == ""
    assert env._aborted is True
    assert env.cleaned_up is True


def test_late_test_injection_paths_are_cleaned_and_never_submitted(
    monkeypatch,
    tmp_path,
):
    env = FakeEnv(diff="diff --git a/tests/leak.py b/tests/leak.py\n+secret test\n")

    async def env_factory(task):
        return env

    async def late_apply_test_patch(_env, _patch):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            return ["tests/leak.py"]

    monkeypatch.setattr(evaluator, "apply_test_patch", late_apply_test_patch)

    result = run(
        run_eval_task(
            EvalTask(
                task_id="late-test-injection",
                description="x",
                timeout=0.02,
                extras={"test_patch": "diff --git a/tests/leak.py b/tests/leak.py"},
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.05,
        )
    )

    assert result.patch == ""
    assert result.test_patch_isolation_failed is True
    assert result.injected_path_cleanup_proven is True
    assert result.task_stage_integrity_proven is False
    assert result.submission_eligible is False
    assert any(
        command == "git --literal-pathspecs checkout -- tests/leak.py"
        for command in env.cmds
    )
    assert any(
        command == "git --literal-pathspecs clean -fq -- tests/leak.py"
        for command in env.cmds
    )


def test_non_quiescent_workflow_bounds_checkpoint_abort_and_revokes_env(
    monkeypatch, tmp_path
):
    release_workflow = asyncio.Event()
    workflow_cancelled = asyncio.Event()
    workflow_finished = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        async def abort(self):
            await super().abort()

    class StubbornCheckpoint:
        def __init__(self, *args, **kwargs):
            self.abort_calls = []

        async def start(self, env, *, exclude_paths=()):
            return None

        async def abort(self, *, timeout):
            self.abort_calls.append(timeout)
            return False

    monkeypatch.setattr(evaluator, "WorktreeCheckpoint", StubbornCheckpoint)
    env = AbortTrackingEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        while not release_workflow.is_set():
            try:
                await release_workflow.wait()
            except asyncio.CancelledError:
                workflow_cancelled.set()
        workflow_finished.set()

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-checkpoint", description="x", timeout=0.01),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                checkpoint_interval_seconds=0.001,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(workflow_cancelled.wait(), timeout=0.5)
        result = await asyncio.wait_for(eval_task, timeout=0.5)
        release_workflow.set()
        await asyncio.wait_for(workflow_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env._aborted is True
    assert result.patch == ""
    assert result.checkpoint_result["abort"]["status"] == "checkpoint_abort_timed_out"
    assert "checkpoint abort timed out" in result.error
    assert result.execution_quiesced is False
    assert result.submission_eligible is False


def test_checkpoint_finalization_is_bounded_when_periodic_capture_stalls(tmp_path):
    class StubbornCaptureEnv(CheckpointEnv):
        def __init__(self):
            super().__init__()
            self.capture_started = asyncio.Event()
            self.release_capture = asyncio.Event()
            self.capture_finished = asyncio.Event()
            self.cancellations = 0

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd) and not self.capture_finished.is_set():
                self.capture_started.set()
                while not self.release_capture.is_set():
                    try:
                        await self.release_capture.wait()
                    except asyncio.CancelledError:
                        self.cancellations += 1
                self.capture_finished.set()
                self._ensure_active()
            return await super().exec_cmd(cmd, timeout)

    env = StubbornCaptureEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        await asyncio.wait_for(env.capture_started.wait(), timeout=0.5)
        return "done"

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stalled-final-checkpoint", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                checkpoint_interval_seconds=0.001,
                cancellation_cleanup_timeout=0.01,
            )
        )
        result = await asyncio.wait_for(eval_task, timeout=0.7)
        env.release_capture.set()
        await asyncio.wait_for(env.capture_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env.cancellations >= 2
    assert env._aborted is True
    assert result.patch == ""
    assert result.checkpoint_result["final"]["status"] == (
        "checkpoint_finalization_timed_out"
    )
    assert "checkpoint finalization timed out" in result.error
    assert result.execution_quiesced is False
    assert result.submission_eligible is False


# --------------------------------------------------------------------------- #
# workflow=None: unchanged single-session path
# --------------------------------------------------------------------------- #


def test_workflow_none_path_unchanged(monkeypatch, tmp_path):
    from opencollab.adapters.llm import LLMResponse, Usage
    from opencollab.bootstrap import container

    class FakeLLMClient:
        def __init__(self, *a, **k):
            pass

        async def complete(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="done",
                tool_calls=[],
                usage=Usage(input_tokens=3, output_tokens=2),
                finish_reason="stop",
            )

    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="t3", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None


# --------------------------------------------------------------------------- #
# generate_review_fix stage skipping
# --------------------------------------------------------------------------- #


class ScriptedCtx:
    """A minimal WorkflowContext stand-in scripting agent() replies."""

    def __init__(self, env: Any, replies: list[Any]) -> None:
        self.env = env
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label}
        )
        return self._replies.pop(0)

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        pass


def test_generate_review_fix_skips_apply_when_ok(tmp_path):
    env = FakeEnv()
    # Stage 1 implement -> text; stage 2 review verdict -> needs_changes False.
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": False, "feedback": "looks good"},
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Only two agent calls — the apply stage was skipped.
    assert len(ctx.agent_calls) == 2
    # The review call used a schema (structured verdict).
    assert ctx.agent_calls[1]["schema"] is not None
    assert result["needs_changes"] is False


def test_generate_review_fix_runs_apply_when_changes_requested(tmp_path):
    env = FakeEnv()
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": True, "feedback": "rename foo to bar"},
            "applied the feedback",
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Three agent calls — implement, review, apply.
    assert len(ctx.agent_calls) == 3
    assert result["needs_changes"] is True
    # The apply-stage prompt carried the review feedback.
    assert "rename foo to bar" in ctx.agent_calls[2]["prompt"]


def test_generate_review_fix_marks_truncated_diff_unavailable(tmp_path):
    class TruncatedReviewEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return ExecResult(
                returncode=0,
                stdout="diff --git a/x b/x\n+partial secret tail\n",
                stderr="",
                stdout_truncated=True,
                stdout_dropped_bytes=7000,
            )

    ctx = ScriptedCtx(
        TruncatedReviewEnv(),
        replies=[
            "implemented the fix",
            {"needs_changes": False, "feedback": "unavailable"},
        ],
    )

    run(generate_review_fix(ctx, {"description": "fix the bug"}))

    review_prompt = ctx.agent_calls[1]["prompt"]
    assert "diff unavailable" in review_prompt
    assert "stdout dropped 7000 bytes" in review_prompt
    assert "partial secret tail" not in review_prompt
