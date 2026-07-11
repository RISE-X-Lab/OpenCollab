from __future__ import annotations

from asyncio_test_support import assert_cancel_note, assert_cancel_reason
from evaluator_workflow_test_support import (
    Any,
    EvalResult,
    EvalTask,
    FakeEnv,
    FakeSession,
    LLMResponse,
    SessionStore,
    Usage,
    _token_bearing_factory,
    asyncio,
    evaluator,
    json,
    os,
    pytest,
    real_build_session,
    run,
    run_eval_task,
    threading,
)


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
    import opencollab.application.autosave as autosave_mod

    monkeypatch.setattr(autosave_mod, "MAX_CANCELLED_SAVE_WAIT_SECONDS", 0.01)
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


def test_deferred_eval_tracer_stays_retained_when_dependency_misses_deadline():
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
    assert tracer.closed is False
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

    assert_cancel_reason(cancellation, "primary cancellation")
    assert_cancel_note(
        cancellation,
        "workflow manifest failed",
        "eval manifest disk failed",
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
            *,
            prompt,
            budget,
            tools=None,
            isolation=False,
            label=None,
            tool_choice=None,
            thinking=None,
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
        session.state.append_message({"role": "user", "content": "late evaluator mutation"})
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
