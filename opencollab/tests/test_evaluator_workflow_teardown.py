from __future__ import annotations

from evaluator_workflow_test_support import (
    CheckpointEnv,
    EvalTask,
    ExecResult,
    FakeEnv,
    ScriptedCtx,
    asyncio,
    evaluator,
    generate_review_fix,
    is_worktree_diff_cmd,
    run,
    run_eval_task,
)


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
    assert any(command == "git --literal-pathspecs checkout -- tests/leak.py" for command in env.cmds)
    assert any(command == "git --literal-pathspecs clean -fq -- tests/leak.py" for command in env.cmds)


def test_non_quiescent_workflow_bounds_checkpoint_abort_and_revokes_env(monkeypatch, tmp_path):
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
    assert result.checkpoint_result["final"]["status"] == ("checkpoint_finalization_timed_out")
    assert "checkpoint finalization timed out" in result.error
    assert result.execution_quiesced is False
    assert result.submission_eligible is False


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
