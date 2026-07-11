from __future__ import annotations

from evaluator_test_support import (
    EvalTask,
    FakeEnv,
    FakeLLMClient,
    asyncio,
    container,
    evaluator,
    is_worktree_diff_cmd,
    pytest,
    run,
    run_eval_task,
)


def test_caller_cancellation_cleans_environment_before_propagating(monkeypatch, tmp_path):
    started = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(evaluator, "_run_single_session", wait_forever)
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancelled", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert env.cleaned_up is True
    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)


def test_same_tick_caller_and_teardown_self_cancel_still_cleans_environment(
    monkeypatch,
    tmp_path,
):
    teardown_started = asyncio.Event()
    release_teardown = asyncio.Event()
    original_wait = evaluator._wait_for_owned_execution
    calls = 0

    async def quick_session(**kwargs):
        return None

    async def self_cancel_once(tasks, workflow_ctx, *, cleanup_timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            teardown_started.set()
            await release_teardown.wait()
            raise asyncio.CancelledError("inner teardown cancellation")
        return await original_wait(
            tasks,
            workflow_ctx,
            cleanup_timeout=cleanup_timeout,
        )

    monkeypatch.setattr(evaluator, "_run_single_session", quick_session)
    monkeypatch.setattr(
        evaluator,
        "_wait_for_owned_execution",
        self_cancel_once,
    )
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        owner = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="dual-cancel", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        await teardown_started.wait()
        owner.cancel("caller cancellation")
        release_teardown.set()
        with pytest.raises(asyncio.CancelledError):
            await owner

    run(scenario())
    assert env.cleaned_up is True


def test_single_session_timeout_waits_for_cleanup_and_keeps_metrics(monkeypatch, tmp_path):
    cancel_seen = asyncio.Event()
    release_cancel = asyncio.Event()
    env = FakeEnv(diff="diff --git a/x b/x\n+early\n")

    class DelayedCancelSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                await release_cancel.wait()
                self.used_tokens = 17
                self.step_count = 3
                env.diff = "diff --git a/x b/x\n+late-cleanup-write\n"
                raise

    session = DelayedCancelSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="slow-single", description="fix", timeout=0.5),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        try:
            await asyncio.wait_for(cancel_seen.wait(), timeout=2.0)
        except TimeoutError:
            await asyncio.wait({eval_task}, timeout=0.1)
            if eval_task.done():
                result = await eval_task
                raise AssertionError(f"evaluation finished without entering delayed cancellation: {result!r}")
            frames = [
                f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}" for frame in eval_task.get_stack()
            ]
            raise AssertionError(
                "evaluation task did not reach cancellation: "
                f"task={eval_task!r}, frames={frames}, "
                f"awaiting={eval_task.get_coro().cr_await!r}"
            )
        await asyncio.sleep(0)
        assert eval_task.done() is False
        assert not any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
        release_cancel.set()
        return await eval_task

    result = run(scenario())

    assert result.error == "Task timed out after 0.5s"
    assert result.tokens_used == 17
    assert result.steps == 3
    assert "late-cleanup-write" in result.patch
    assert result.submission_eligible is True


def test_non_quiescent_timeout_is_bounded_and_revokes_environment(monkeypatch, tmp_path):
    release_cleanup = asyncio.Event()
    cleanup_started = asyncio.Event()
    late_write_blocked = asyncio.Event()
    finished = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        writes: list[tuple[str, str]] = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv(diff="diff --git a/x b/x\n+untrusted\n")

    class NeverQuiescentSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    continue
            try:
                await env.write_file("late.py", "late")
            except RuntimeError:
                late_write_blocked.set()
            finally:
                finished.set()
            return "late"

    session = NeverQuiescentSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="never-clean", description="fix", timeout=0.01),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await cleanup_started.wait()
        result = await asyncio.wait_for(eval_task, timeout=0.5)
        release_cleanup.set()
        await asyncio.wait_for(finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert late_write_blocked.is_set() is True
    assert env.writes == []
    assert result.patch == ""
    assert result.patch_produced is False
    assert "execution cleanup timed out" in result.error
    assert "patch extraction skipped" in result.error
    assert not any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
    assert result.execution_quiesced is False
    assert result.submission_eligible is False


def test_repeated_caller_cancel_cannot_interrupt_evaluator_teardown(monkeypatch, tmp_path):
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_session = asyncio.Event()
    finished = asyncio.Event()
    late_write_blocked = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv()

    class StubbornSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            started.set()
            while not release_session.is_set():
                try:
                    await release_session.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            try:
                await env.write_file("late.py", "late")
            except RuntimeError:
                late_write_blocked.set()
            finally:
                finished.set()

    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: StubbornSession())

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="double-cancel", description="fix", timeout=60),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        eval_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
        eval_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        release_session.set()
        await asyncio.wait_for(finished.wait(), timeout=0.5)

    run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert late_write_blocked.is_set() is True
    assert env.writes == []


def test_caller_cancel_owns_stubborn_initial_user_message(monkeypatch, tmp_path):
    add_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_add = asyncio.Event()
    add_finished = asyncio.Event()
    late_write_blocked = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv()

    class StubbornAddSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0
        run_loop_called = False

        async def add_user_message(self, content):
            add_started.set()
            while not release_add.is_set():
                try:
                    await release_add.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            try:
                await env.write_file("late-message.py", content)
            except RuntimeError:
                late_write_blocked.set()
            finally:
                add_finished.set()

        async def run_loop(self):
            self.run_loop_called = True

    session = StubbornAddSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-initial-add", description="fix", timeout=60),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(add_started.wait(), timeout=0.5)
        eval_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
        eval_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        release_add.set()
        await asyncio.wait_for(add_finished.wait(), timeout=0.5)

    run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert env.writes == []
    assert late_write_blocked.is_set() is True
    assert session.run_loop_called is False


def test_task_deadline_bounds_stubborn_initial_user_message(monkeypatch, tmp_path):
    add_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_add = asyncio.Event()
    add_finished = asyncio.Event()

    class StubbornAddSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0
        run_loop_called = False

        async def add_user_message(self, content):
            add_started.set()
            while not release_add.is_set():
                try:
                    await release_add.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            add_finished.set()

        async def run_loop(self):
            self.run_loop_called = True

    session = StubbornAddSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        result = await asyncio.wait_for(
            run_eval_task(
                EvalTask(
                    task_id="stubborn-add-deadline",
                    description="fix",
                    timeout=0.01,
                ),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
        release_add.set()
        await asyncio.wait_for(add_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert add_started.is_set() is True
    assert cancellation_seen.is_set() is True
    assert session.run_loop_called is False
    assert env._aborted is True
    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.error and result.error.startswith("Task timed out after 0.01s")


def test_caller_cancel_bounds_stubborn_environment_cleanup(monkeypatch, tmp_path):
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class StubbornCleanupEnv(FakeEnv):
        async def cleanup(self) -> None:
            cleanup_started.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_cancelled.set()
            self.cleaned_up = True
            cleanup_finished.set()

    env = StubbornCleanupEnv()
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-env-cleanup", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        eval_task.cancel()
        eval_task.cancel()
        await asyncio.wait_for(cleanup_cancelled.wait(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        assert env._aborted is True
        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)

    run(scenario())
