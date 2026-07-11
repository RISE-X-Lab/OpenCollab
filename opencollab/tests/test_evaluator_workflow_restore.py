from __future__ import annotations

from evaluator_workflow_test_support import (
    CheckpointEnv,
    EvalTask,
    ExecResult,
    WorktreeCheckpoint,
    asyncio,
    checkpoint_mod,
    is_worktree_diff_cmd,
    json,
    pytest,
    run,
    run_eval_task,
    seed_checkpoint,
)


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
    assert result.error == ("checkpoint recovery apply failed: OSError: apply transport broke")
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

    result = run(checkpoint.restore_latest(RemovalFailureEnv(diff_outputs=[""])))

    assert result.status == "failed"
    assert result.submission_eligible is False
    assert result.error == ("checkpoint recovery temporary-file cleanup failed: OSError: cannot unlink recovery patch")


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


@pytest.mark.asyncio
async def test_checkpoint_restore_cancelled_partial_apply_marks_cancellation_unproven(
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
        await checkpoint.restore_latest(CancelledPartialEnv())

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
    assert result.checkpoint_result["final"]["status"] == ("skipped_checkpoint_restore_integrity_failure")
    assert result.patch == ""
    assert result.patch_produced is False
    assert result.submission_eligible is False
