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
import json
import os
from typing import Any

from opencollab.adapters.env import Environment, ExecResult
from opencollab.harness.evaluator import EvalResult, EvalTask, run_eval_task
from opencollab.harness.workflows import generate_review_fix


def run(coro):
    return asyncio.run(coro)


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
    run_dir.mkdir(parents=True)
    checkpoint_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n"
    (run_dir / "checkpoint.worktree.patch").write_text(checkpoint_patch, encoding="utf-8")

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
    test_injection_index = next(i for i, cmd in enumerate(env.cmds) if "opencollab_test_patch" in cmd)
    assert restore_index < test_injection_index
    assert result.checkpoint_result["restore"]["status"] == "restored"


def test_workflow_checkpoint_restore_skips_dirty_worktree(tmp_path):
    env = CheckpointEnv(diff_outputs=["diff --git a/dirty b/dirty\n+dirty\n"])

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "dirty"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.worktree.patch").write_text(
        "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n",
        encoding="utf-8",
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


def test_workflow_checkpoint_restore_uses_nonempty_patch_when_metadata_is_corrupt(tmp_path):
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

    assert result.checkpoint_result["restore"]["status"] == "restored"
    recovery_path, recovery_content = env.writes[0]
    assert recovery_path.startswith("/tmp/opencollab-checkpoint-recovery-")
    assert recovery_path.endswith(".patch")
    assert recovery_content == checkpoint_patch


def test_workflow_checkpoint_restore_path_is_private_per_run_dir(tmp_path):
    first_env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])
    second_env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])

    async def wf(ctx, args):
        return {"status": "done"}

    for task_id, env in (("first", first_env), ("second", second_env)):
        run_dir = tmp_path / "trajectories" / task_id
        run_dir.mkdir(parents=True)
        (run_dir / "checkpoint.worktree.patch").write_text(
            "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n",
            encoding="utf-8",
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
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.worktree.patch").write_text(
        "diff --git a/pkg/a.py b/pkg/a.py\n+old\n",
        encoding="utf-8",
    )
    (run_dir / "checkpoint.worktree.json").write_text(
        json.dumps({"submission_eligible": False, "preserved_previous_patch": True}),
        encoding="utf-8",
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
    assert "git reset -q HEAD -- tests/test_x.py" in checkpoint_cmds[-1]


def test_workflow_checkpoint_excludes_own_artifacts_inside_workspace(tmp_path):
    env = CheckpointEnv(diff_outputs=["", "diff --git a/x b/x\n+after\n"])
    env.workspace = str(tmp_path)

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    output_dir = tmp_path / "eval_results"
    run_dir = output_dir / "trajectories" / "inside"
    run_dir.mkdir(parents=True)
    checkpoint_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+restored\n"
    (run_dir / "checkpoint.worktree.patch").write_text(checkpoint_patch, encoding="utf-8")

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
    assert "git reset -q HEAD -- eval_results/trajectories/inside/checkpoint.worktree.patch" in checkpoint_cmds[0]
    assert "git reset -q HEAD -- eval_results/trajectories/inside/checkpoint.worktree.json" in checkpoint_cmds[0]


def test_workflow_checkpoint_capture_failure_preserves_previous_patch(tmp_path):
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


def test_workflow_abnormal_exit_records_error_but_keeps_metrics(tmp_path):
    """An outer-wall timeout / crash keeps the partial patch + metrics, records cause.

    The lost django-11564 run was an outer-wall ``asyncio.TimeoutError``. Such an
    ending must still surface real metrics and the on-disk patch, with the cause
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
    assert result.error is not None and "timed out" in result.error.lower()


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
