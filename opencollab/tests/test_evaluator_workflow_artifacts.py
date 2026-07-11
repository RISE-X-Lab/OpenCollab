from __future__ import annotations

from evaluator_workflow_test_support import (
    CheckpointEnv,
    EvalTask,
    LocalEnvironment,
    WorktreeCheckpoint,
    evaluator,
    is_worktree_diff_cmd,
    json,
    run,
    run_eval_task,
    seed_checkpoint,
    subprocess,
)


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
    test_injection_index = next(i for i, cmd in enumerate(env.cmds) if "opencollab-test-patch-" in cmd)
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

    assert result.checkpoint_result["restore"]["status"] == ("failed_metadata_integrity")
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

    checkpoint_cmds = [cmd for cmd in env.cmds if is_worktree_diff_cmd(cmd)]
    assert checkpoint_cmds
    assert (
        'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q HEAD -- tests/test_x.py'
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

    checkpoint_cmds = [cmd for cmd in env.cmds if is_worktree_diff_cmd(cmd)]
    assert checkpoint_cmds
    assert result.checkpoint_result["restore"]["status"] == "restored"
    assert (
        'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q HEAD -- '
        "eval_results/trajectories/inside/checkpoint.worktree.patch" in checkpoint_cmds[0]
    )
    assert (
        'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q HEAD -- '
        "eval_results/trajectories/inside/checkpoint.worktree.json" in checkpoint_cmds[0]
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
