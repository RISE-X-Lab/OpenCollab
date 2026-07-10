from __future__ import annotations

from evaluator_test_support import (
    Environment,
    EvalTask,
    ExecResult,
    FakeEnv,
    InjectFakeEnv,
    asyncio,
    is_worktree_diff_cmd,
    run,
    run_eval_task,
)


def test_diff_exclusion_omits_injected_test_paths(tmp_path):
    # Injected test_patch that MODIFIES an existing tracked test file.
    env = InjectFakeEnv(mod_path="tests/test_app.py")

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
                        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                    ),
                    "fail_to_pass": ["tests/test_app.py::test_thing"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected test path was checked out before extraction.
    assert "tests/test_app.py" in env.checked_out
    checkout_cmds = [c for c in env.cmds if c.startswith("git --literal-pathspecs checkout --")]
    assert checkout_cmds and "tests/test_app.py" in checkout_cmds[0]
    # The submitted patch contains the source edit but NOT the injected test.
    assert "src/app.py" in result.patch
    assert "tests/test_app.py" not in result.patch
    # The workflow saw fail_to_pass and the injected paths in its args dict.
    assert seen["args"]["fail_to_pass"] == ["tests/test_app.py::test_thing"]
    assert seen["args"]["injected_test_paths"] == ["tests/test_app.py"]


def test_diff_exclusion_omits_injected_new_test_file(tmp_path):
    # Regression for the new-file leak: SWE-bench test_patches commonly ADD a new
    # test file. `git checkout --` cannot remove an untracked file (errors rc=1);
    # the production exclusion must also `git clean -fq` it. The submitted patch is
    # extracted with `git add -A && git diff --cached`, so a surviving new file
    # would otherwise be staged and leak -> double-apply at grading time (D1).
    env = InjectFakeEnv(new_path="tests/test_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-new",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected new file was cleaned (checkout alone cannot remove it).
    assert "tests/test_new.py" in env.cleaned
    # The submitted patch contains the source edit but NOT the injected new test.
    assert "src/app.py" in result.patch
    assert "tests/test_new.py" not in result.patch


def test_diff_exclusion_one_new_file_does_not_strand_other_injected_edits(tmp_path):
    # A mixed test_patch (new file + modified existing file) must not let the new
    # file abort reverting the rest. The old single-command `git checkout -- p1 p2`
    # aborted entirely (rc=1) on the untracked path, stranding the tracked edit in
    # the submitted patch. Per-path revert keeps each independent.
    env = InjectFakeEnv(mod_path="tests/test_exist.py", new_path="tests/test_brand_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-mixed",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_exist.py b/tests/test_exist.py\n"
                        "--- a/tests/test_exist.py\n+++ b/tests/test_exist.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                        "diff --git a/tests/test_brand_new.py b/tests/test_brand_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_brand_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_brand_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # Both injected paths reverted: the tracked mod checked out, the new file cleaned.
    assert "tests/test_exist.py" in env.checked_out
    assert "tests/test_brand_new.py" in env.cleaned
    # Neither injected test leaks; only the source edit remains.
    assert "src/app.py" in result.patch
    assert "tests/test_exist.py" not in result.patch
    assert "tests/test_brand_new.py" not in result.patch


def test_failed_injected_path_cleanup_is_reported(tmp_path):
    class CleanupFailureEnv(InjectFakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs clean -fq -- "):
                self.cmds.append(cmd)
                return ExecResult(returncode=1, stdout="", stderr="clean failed")
            return await super().exec_cmd(cmd, timeout)

    env = CleanupFailureEnv(new_path="tests/test_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="cleanup-injected",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n"
                        "@@ -0,0 +1 @@\n+test\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.error == (
        "test patch cleanup failed: RuntimeError: injected path still dirty: tests/test_new.py: ?? tests/test_new.py"
    )
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False


def test_injected_path_cleanup_aggregate_deadline_invalidates_submission(tmp_path):
    class SlowCleanupEnv(InjectFakeEnv):
        def __init__(self):
            super().__init__(mod_path="tests/test_slow.py")
            self.cleanup_timeouts: list[float] = []

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs checkout -- "):
                self.cleanup_timeouts.append(timeout)
                await asyncio.sleep(0.01)
            return await super().exec_cmd(cmd, timeout)

    env = SlowCleanupEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="cleanup-deadline",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_slow.py b/tests/test_slow.py\n"
                        "--- a/tests/test_slow.py\n"
                        "+++ b/tests/test_slow.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+test\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            cancellation_cleanup_timeout=0.005,
        )
    )

    assert env.cleanup_timeouts
    assert 0 < env.cleanup_timeouts[0] <= 0.005
    assert result.patch_produced is True
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False
    assert "aggregate injected-path cleanup deadline expired" in result.error


def test_failed_partial_test_patch_rollback_stops_agent_and_invalidates_output(tmp_path):
    class IsolationFailureEnv(Environment):
        def __init__(self):
            self.cmds: list[str] = []
            self.cleaned_up = False

        async def write_file(self, path: str, content: str) -> None:
            return None

        async def write_temp_file(
            self,
            content: str,
            *,
            prefix: str,
            suffix: str = ".tmp",
        ) -> str:
            path = f"/tmp/{prefix}isolation{suffix}"
            await self.write_file(path, content)
            return path

        async def remove_file(self, path: str) -> None:
            return None

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            self.cmds.append(cmd)
            if cmd.startswith("git apply --numstat"):
                return ExecResult(
                    returncode=0,
                    stdout="1\t1\ttests/test_new.py\0",
                    stderr="",
                )
            if cmd.startswith("git apply --check"):
                return ExecResult(returncode=0, stdout="", stderr="")
            if cmd.startswith("git apply -v"):
                raise OSError("git apply transport failed after partial mutation")
            if cmd.startswith("patch --dry-run"):
                return ExecResult(returncode=0, stdout="", stderr="")
            if cmd.startswith("patch -p1"):
                return ExecResult(returncode=1, stdout="", stderr="partial apply")
            if cmd.startswith("git --literal-pathspecs"):
                return ExecResult(returncode=1, stdout="", stderr="rollback failed")
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/tests/test_new.py b/tests/test_new.py\n+leak\n",
                    stderr="",
                )
            return ExecResult(returncode=0, stdout="", stderr="")

        async def cleanup(self) -> None:
            self.cleaned_up = True

    env = IsolationFailureEnv()
    workflow_ran = False
    checkpoint_dir = tmp_path / "trajectories" / "partial-injection-rollback"
    checkpoint_dir.mkdir(parents=True)
    old_checkpoint = "diff --git a/src/old.py b/src/old.py\n+preserved\n"
    checkpoint_path = checkpoint_dir / "checkpoint.worktree.patch"
    checkpoint_path.write_text(old_checkpoint, encoding="utf-8")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="partial-injection-rollback",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "--- a/tests/test_new.py\n"
                        "+++ b/tests/test_new.py\n"
                        "@@ -1 +1 @@\n-old\n+injected\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    assert workflow_ran is False
    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.patch_produced is False
    assert result.test_patch_isolation_failed is True
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False
    assert "TestPatchIsolationError" in result.error
    diff_commands = [cmd for cmd in env.cmds if is_worktree_diff_cmd(cmd)]
    assert len(diff_commands) == 1
    assert result.checkpoint_result["final"]["status"] == ("skipped_test_patch_isolation_failure")
    assert checkpoint_path.read_text(encoding="utf-8") == old_checkpoint
    diff_command = diff_commands[0]
    assert "git --literal-pathspecs reset -q HEAD -- tests/test_new.py" in diff_command
    assert "tests/test_new.py.orig" not in diff_command
    assert "tests/test_new.py.rej" not in diff_command


def test_workflow_args_preserve_fail_to_pass_when_not_injected(tmp_path):
    # A missing test patch must not erase the declared verification targets.
    # The workflow may prove that the targets already exist and pass; otherwise
    # its FAIL_TO_PASS gate must keep the result red.
    env = FakeEnv()

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    run(
        run_eval_task(
            EvalTask(
                task_id="t-f2p",
                description="x",
                extras={"fail_to_pass": ["pkg::test_a", "pkg::test_b"]},
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert seen["args"]["fail_to_pass"] == ["pkg::test_a", "pkg::test_b"]
    assert "injected_test_paths" not in seen["args"]
