from __future__ import annotations

from asyncio_test_support import assert_cancel_note, assert_cancel_reason
from evaluator_workflow_test_support import (
    Any,
    CheckpointEnv,
    Environment,
    EvalTask,
    ExecResult,
    FakeEnv,
    FakeSession,
    LocalEnvironment,
    WorktreeCheckpoint,
    _token_bearing_factory,
    asyncio,
    checkpoint_mod,
    hashlib,
    is_worktree_diff_cmd,
    json,
    os,
    pytest,
    run,
    run_eval_task,
    subprocess,
)


def test_checkpoint_never_maps_host_artifacts_into_non_local_workspace():
    class NonLocalEnv(Environment):
        workspace = "/testbed"
        local_filesystem = False

    checkpoint = WorktreeCheckpoint("/testbed/eval_results/trajectories/container-task")

    assert checkpoint._artifact_exclude_paths(NonLocalEnv()) == ()


def _git_patch_context(repo):
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return base, os.path.join(git_dir, "objects")


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
    base, objects = _git_patch_context(repo)

    try:
        result = subprocess.run(
            [
                "bash",
                "-lc",
                checkpoint_mod.worktree_diff_command(
                    ["harness.tmp"],
                    base_revision=base,
                    object_directory=objects,
                    working_tree=str(repo),
                ),
            ],
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
    command = checkpoint_mod.worktree_diff_command(
        ["harness.tmp"],
        base_revision="0" * 40,
        object_directory="/tmp/opencollab-test-objects",
        working_tree="/tmp/opencollab-test-worktree",
    )

    assert "|| true" not in command
    assert 'git --literal-pathspecs reset -q ' in command
    assert "unregistered or modified .opencollab-retired-*" in command
    assert 'git diff --cached --binary ' in command


def test_worktree_diff_excludes_registered_tombstone_and_keeps_real_change(
    tmp_path,
):
    from opencollab.adapters.retirement_registry import (
        register_verified_retirement,
        registered_retirement_paths,
    )

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
    tombstone = repo / ".opencollab-retired-framework"
    tombstone.write_text("old\n", encoding="utf-8")
    parent_fd = os.open(repo, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        register_verified_retirement(parent_fd, tombstone.name)
    finally:
        os.close(parent_fd)

    base, objects = _git_patch_context(repo)
    command = checkpoint_mod.worktree_diff_command(
        registered_retirement_paths=registered_retirement_paths(repo),
        base_revision=base,
        object_directory=objects,
        working_tree=str(repo),
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "source.py" in result.stdout
    assert tombstone.name not in result.stdout


def test_worktree_diff_rejects_unregistered_reserved_prefix(tmp_path):
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
    (repo / ".opencollab-retired-model").write_text("hidden\n", encoding="utf-8")
    base, objects = _git_patch_context(repo)

    result = subprocess.run(
        [
            "bash",
            "-lc",
            checkpoint_mod.worktree_diff_command(
                base_revision=base,
                object_directory=objects,
                working_tree=str(repo),
            ),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 125
    assert "unregistered or modified .opencollab-retired-*" in result.stderr


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
            self.workspace = str(repo)

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
            if is_worktree_diff_cmd(cmd):
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
    assert result.error == ("TimeoutError: provider timeout; tracer close failed: OSError: trace disk failure")


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

    assert_cancel_reason(cancellation, "primary cancellation")
    assert_cancel_note(
        cancellation,
        "environment cleanup failed",
        "environment cleanup exploded",
    )
