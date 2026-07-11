from __future__ import annotations

from evaluator_test_support import (
    EvalTask,
    ExecResult,
    FakeEnv,
    FakeLLMClient,
    asyncio,
    container,
    evaluator,
    gc,
    is_worktree_diff_cmd,
    pytest,
    run,
    run_eval_task,
    sys,
)


@pytest.mark.parametrize(
    "extras",
    [
        ["not", "a", "dict"],
        {"test_patch": 1},
    ],
    ids=["non-dict", "non-string-test-patch"],
)
def test_run_eval_task_rejects_invalid_extras_before_side_effects(
    extras,
    tmp_path,
):
    output_dir = tmp_path / "output"
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    with pytest.raises(ValueError, match="task extras"):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-extras", description="fix", extras=extras),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


def test_tracer_close_failure_still_cleans_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    real_tracer = evaluator.Tracer

    class FailingCloseTracer:
        def __init__(self, *args, **kwargs):
            self._inner = real_tracer(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            raise OSError("trace disk failure")

    monkeypatch.setattr(evaluator, "Tracer", FailingCloseTracer)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="trace-close", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is True
    assert "tracer close failed: OSError: trace disk failure" in result.error


def test_tracer_destructor_never_emits_an_unraisable_close_failure(monkeypatch):
    unraisable = []

    class DestructorCloseFailure(evaluator.Tracer):
        def __init__(self):
            return None

        def close(self):
            raise KeyboardInterrupt("late close failed")

    tracer = DestructorCloseFailure()
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    del tracer
    gc.collect()

    assert unraisable == []


def test_patch_extraction_exception_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class ExtractionFailureEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                raise OSError("temporary index unavailable")
            return await super().exec_cmd(cmd, timeout)

    env = ExtractionFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-exception", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is False
    assert result.error == ("patch extraction failed: OSError: temporary index unavailable")
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_patch_extraction_nonzero_exit_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class ExtractionFailureEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(returncode=128, stdout="", stderr="index locked")
            return await super().exec_cmd(cmd, timeout)

    env = ExtractionFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-nonzero", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is False
    assert result.error == ("patch extraction failed: RuntimeError: diff command exited 128: index locked")
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_patch_extraction_rejects_truncated_diff(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class TruncatedDiffEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/x b/x\n+partial\n",
                    stderr="",
                    stdout_truncated=True,
                    stdout_dropped_bytes=8192,
                )
            return await super().exec_cmd(cmd, timeout)

    env = TruncatedDiffEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-truncated", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch == ""
    assert result.patch_produced is False
    assert "patch extraction failed" in result.error
    assert "stdout dropped 8192 bytes" in result.error
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_environment_cleanup_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self) -> None:
            raise OSError("container removal failed")

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-failure", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch_produced is False
    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert result.error == ("environment cleanup failed: OSError: container removal failed")
    assert env._aborted is True


def test_environment_cleanup_exception_invokes_abort_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.abort_calls = 0

        async def cleanup(self) -> None:
            raise OSError("container removal failed")

        async def abort(self) -> None:
            self.abort_calls += 1

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-failure-abort", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env._aborted is True
    assert env.abort_calls == 1
    assert "environment cleanup failed: OSError: container removal failed" in result.error
    assert "environment abort" not in result.error


def test_environment_cleanup_and_abort_exceptions_are_both_reported(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupAndAbortFailureEnv(FakeEnv):
        async def cleanup(self) -> None:
            raise OSError("cleanup exploded")

        async def abort(self) -> None:
            raise RuntimeError("abort exploded")

    env = CleanupAndAbortFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-and-abort-failure", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env._aborted is True
    assert "environment cleanup failed: OSError: cleanup exploded" in result.error
    assert "environment abort failed: RuntimeError: abort exploded" in result.error


def test_environment_cleanup_exception_and_stubborn_abort_are_bounded(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureBlockingAbortEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.release_abort = asyncio.Event()
            self.abort_finished = asyncio.Event()

        async def cleanup(self) -> None:
            raise OSError("cleanup exploded")

        async def abort(self) -> None:
            try:
                while not self.release_abort.is_set():
                    try:
                        await self.release_abort.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                self.abort_finished.set()

    env = CleanupFailureBlockingAbortEnv()

    async def env_factory(task):
        return env

    async def scenario():
        result = await asyncio.wait_for(
            run_eval_task(
                EvalTask(task_id="cleanup-failure-blocking-abort", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
        env.release_abort.set()
        await asyncio.wait_for(env.abort_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env._aborted is True
    assert "environment cleanup failed: OSError: cleanup exploded" in result.error
    assert "environment abort timed out" in result.error


def test_cancelled_environment_cleanup_is_reported_as_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class BlockingCleanupEnv(FakeEnv):
        async def cleanup(self) -> None:
            await asyncio.Event().wait()

    env = BlockingCleanupEnv()

    async def env_factory(task):
        return env

    result = run(
        asyncio.wait_for(
            run_eval_task(
                EvalTask(task_id="blocked-cleanup", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
    )

    assert result.patch_produced is False
    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert env._aborted is True
    assert "environment cleanup timed out" in result.error
