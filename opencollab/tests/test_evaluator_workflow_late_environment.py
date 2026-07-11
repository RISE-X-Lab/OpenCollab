"""Late-environment lifecycle regressions for evaluator workflows."""

from evaluator_workflow_test_support import (
    EvalTask,
    FakeEnv,
    asyncio,
    run,
    run_eval_task,
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
