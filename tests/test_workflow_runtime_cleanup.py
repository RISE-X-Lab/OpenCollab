"""Workflow runtime cleanup and quiescence tests."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import pytest
from asyncio_test_support import assert_cancel_note, assert_cancel_reason
from workflow_runtime_test_support import (
    _cfg,
    _patch_build_session,
)

from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap import (
    _workflow_runtime_cleanup as workflow_cleanup,
)
from opencollab.bootstrap import (
    _workflow_runtime_execution as workflow_execution,
)
from opencollab.bootstrap import (
    workflow_runtime,
)
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.events import SessionRuntimeEvent


@pytest.mark.asyncio
async def test_cleanup_cancels_tasks_without_aborting_caller_environment():
    class CallerEnvironment:
        def __init__(self) -> None:
            self.revoked = False
            self.abort_calls = 0

        def revoke(self) -> None:
            self.revoked = True

        async def abort(self) -> None:
            self.abort_calls += 1
            self.revoked = True

    class Session:
        def __init__(self, environment, cleanup_task) -> None:
            self.env = environment
            self.pending_cleanup_tasks = (cleanup_task,)
            self.persistence_errors: tuple[str, ...] = ()

    environment = CallerEnvironment()
    cleanup_task = asyncio.create_task(asyncio.Event().wait())
    ctx = WorkflowContext(factory=object())
    ctx._cleanup_environment = False
    ctx._track_session(Session(environment, cleanup_task))

    quiesced, succeeded, lingering = await workflow_cleanup._quiesce_workflow_context(
        ctx,
        timeout=0.01,
    )

    assert quiesced
    assert succeeded
    assert not lingering
    assert not environment.revoked
    assert environment.abort_calls == 0


@pytest.mark.asyncio
async def test_workflow_finalization_closes_session_resources():
    class ClosableSession:
        pending_cleanup_tasks = ()
        persistence_errors = ()

        def __init__(self):
            self.close_calls = 0

        def enqueue_auto_save(self):
            return None

        async def aclose(self):
            self.close_calls += 1

    session = ClosableSession()
    ctx = WorkflowContext(factory=object())
    ctx._track_session(session)

    quiesced, succeeded, lingering = (
        await workflow_cleanup._quiesce_and_finalize_workflow_context(
            ctx,
            timeout=0.1,
        )
    )

    assert quiesced
    assert succeeded
    assert not lingering
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_run_workflow_quiesces_late_session_before_manifest_and_tracer_close(
    monkeypatch,
    tmp_path,
):
    order: list[str] = []
    tracer_instances: list[Any] = []
    sentinel = tmp_path / "late-write.txt"
    holder: dict[str, Any] = {}

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            tracer_instances.append(self)

        def log_step(self, step_type, payload, tokens=0, latency=0.0) -> None:
            assert self.closed is False
            if step_type == "late_cleanup":
                order.append("late-trace")

        def close(self) -> None:
            self.closed = True
            order.append("tracer-close")

    class RevocableEnvironment:
        def __init__(self) -> None:
            self._aborted = False
            self.abort_called = False
            self.blocked_writes = 0

        async def write_file(self, path: str, content: str) -> None:
            if self._aborted:
                self.blocked_writes += 1
                raise RuntimeError("environment revoked")
            sentinel.write_text(content, encoding="utf-8")

        async def abort(self) -> None:
            self.abort_called = True
            self._aborted = True

    class CancellationResistantSession:
        used_tokens = 0

        def __init__(self, environment, tracer) -> None:
            self.env = environment
            self.tracer = tracer
            self.cancel_seen = asyncio.Event()

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.env._aborted:
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        continue
                self.tracer.log_step("late_cleanup", {})
                try:
                    await self.env.write_file(str(sentinel), "too late")
                except RuntimeError:
                    pass
                order.append("session-finished")
                return "late"

    class OneSessionFactory:
        def __init__(self, session) -> None:
            self.session = session

        def build_workflow_session(self, **kwargs):
            return self.session

    def fake_build_context(**kwargs):
        environment = RevocableEnvironment()
        session = CancellationResistantSession(environment, kwargs["tracer"])
        holder.update(environment=environment, session=session)
        return WorkflowContext(
            OneSessionFactory(session),
            tracer=kwargs["tracer"],
            max_concurrency=kwargs["max_concurrency"],
            task_concurrency=kwargs["task_concurrency"],
            budget_total=kwargs["budget"],
        )

    original_manifest = workflow_cleanup._write_workflow_manifest

    def recording_manifest(*args, **kwargs):
        order.append("manifest")
        return original_manifest(*args, **kwargs)

    monkeypatch.setattr(workflow_execution, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_execution, "build_workflow_context", fake_build_context)
    monkeypatch.setattr(
        workflow_cleanup,
        "_write_workflow_manifest",
        recording_manifest,
    )

    async def fn(ctx, args):
        assert await ctx.agent("slow", timeout=0.001) is None
        await asyncio.wait_for(holder["session"].cancel_seen.wait(), timeout=0.5)
        return "ok"

    save_dir = str(tmp_path / "run")
    result = await workflow_runtime.run_workflow(
        fn,
        {},
        cfg=_cfg(),
        save_dir=save_dir,
        cleanup_timeout=0.5,
    )

    assert result == "ok"
    assert holder["environment"].abort_called is True
    assert holder["environment"].blocked_writes == 1
    assert sentinel.exists() is False
    assert tracer_instances[0].closed is True
    assert order.index("session-finished") < order.index("manifest")
    assert order.index("session-finished") < order.index("tracer-close")
    assert os.path.exists(os.path.join(save_dir, "workflow.json"))

@pytest.mark.asyncio
async def test_workflow_cleanup_marks_cancelled_blocking_autosave_nonquiescent():
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []
    call_count = 0

    def blocking_save() -> None:
        nonlocal call_count
        call_count += 1
        current = call_count
        calls.append(f"start-{current}")
        if current == 1:
            first_started.set()
            assert release_first.wait(timeout=2.0)
        else:
            second_started.set()
            assert release_second.wait(timeout=2.0)
        calls.append(f"end-{current}")

    subscriber = AutoSaveSubscriber(blocking_save)
    event_bus = EventBus(subscriber)

    class SessionWithOwnedAutosave:
        @property
        def pending_cleanup_tasks(self):
            return event_bus.pending_tasks

    class UnusedFactory:
        def build_workflow_session(self, **kwargs):
            raise AssertionError("session creation is not used")

    ctx = WorkflowContext(UnusedFactory())
    ctx._track_session(SessionWithOwnedAutosave())

    first_waiter = asyncio.create_task(
        event_bus.emit(SessionRuntimeEvent(type="step_end"))
    )
    assert await asyncio.to_thread(first_started.wait, 1.0)
    second_waiter = asyncio.create_task(
        event_bus.emit(SessionRuntimeEvent(type="step_end"))
    )
    waiter_results = await asyncio.gather(
        first_waiter,
        second_waiter,
        return_exceptions=True,
    )
    assert waiter_results == [None, None]
    owners = subscriber.pending_tasks
    assert len(owners) == 1
    assert event_bus.pending_tasks == owners

    cleanup = asyncio.create_task(
        workflow_cleanup._quiesce_workflow_context(ctx, timeout=0.02)
    )
    deadline = asyncio.get_running_loop().time() + 0.5
    while not (
        cleanup.done()
        or all(owner.cancelled() or owner.done() for owner in owners)
        or subscriber.failure_count > 0
    ):
        assert not cleanup.done()
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)

    try:
        release_first.set()
        assert await asyncio.to_thread(second_started.wait, 0.5)
        quiesced, succeeded, _lingering = await cleanup
        assert quiesced is False
        assert succeeded is False
        assert calls == ["start-1", "end-1", "start-2"]
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*owners, return_exceptions=True)

    assert calls == ["start-1", "end-1", "start-2", "end-2"]
    assert await workflow_cleanup._wait_for_context_cleanup(ctx, timeout=0.2)

@pytest.mark.asyncio
async def test_workflow_cleanup_reports_completed_autosave_failure():
    error = OSError("snapshot disk full")

    def failed_save() -> None:
        raise error

    subscriber = AutoSaveSubscriber(failed_save)
    event_bus = EventBus(subscriber)

    class SessionWithFailedAutosave:
        @property
        def pending_cleanup_tasks(self):
            return event_bus.pending_tasks

        @property
        def persistence_errors(self):
            return (subscriber.last_error,) if subscriber.last_error else ()

    class UnusedFactory:
        def build_workflow_session(self, **kwargs):
            raise AssertionError("session creation is not used")

    ctx = WorkflowContext(UnusedFactory())
    ctx._track_session(SessionWithFailedAutosave())
    await event_bus.emit(SessionRuntimeEvent(type="step_end"))

    quiesced, succeeded, _lingering = await workflow_cleanup._quiesce_workflow_context(
        ctx,
        timeout=0.1,
    )
    assert quiesced is True
    assert succeeded is False
    assert subscriber.last_error is error

@pytest.mark.asyncio
async def test_workflow_cleanup_tracks_abandoned_provider_task_until_exit():
    class CancellationResistantLLM:
        def __init__(self):
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                raise

    llm = CancellationResistantLLM()
    session = build_session(
        agent=Agent(
            name="workflow_agent",
            system_prompt="sys",
            tools=[],
            model="test-model",
            provider="test",
        ),
        llm=llm,
        llm_timeout=0.01,
    )

    class OneSessionFactory:
        def build_workflow_session(self, **kwargs):
            return session

    ctx = WorkflowContext(OneSessionFactory())
    assert await ctx.agent("trigger provider timeout") is None
    assert llm.cancel_seen.is_set()
    assert session.pending_cleanup_tasks

    quiesced, succeeded, _lingering = await workflow_cleanup._quiesce_workflow_context(
        ctx,
        timeout=0.01,
    )
    assert quiesced is False
    assert succeeded is False
    pending = session.pending_cleanup_tasks
    assert pending

    llm.release.set()
    await asyncio.gather(*pending, return_exceptions=True)
    assert await workflow_cleanup._wait_for_context_cleanup(ctx, timeout=0.2)

@pytest.mark.asyncio
async def test_run_workflow_reports_technical_cleanup_failure_after_quiescence(
    monkeypatch,
    tmp_path,
):
    calls = _patch_build_session(monkeypatch)
    closed: list[bool] = []

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

    async def failed_abort(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(workflow_execution, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_cleanup, "_quiesce_workflow_context", failed_abort)

    async def fn(ctx, args):
        await ctx.agent("done")
        return "would otherwise succeed"

    save_dir = str(tmp_path / "run")
    with pytest.raises(RuntimeError, match="technical workflow cleanup failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=save_dir,
            cleanup_timeout=0.01,
        )

    assert calls
    assert closed == [True]
    assert os.path.exists(os.path.join(save_dir, "workflow.json")) is False

@pytest.mark.asyncio
async def test_run_workflow_cleanup_failure_keeps_workflow_failure_as_cause(
    monkeypatch,
):
    _patch_build_session(monkeypatch)

    async def failed_cleanup(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(
        workflow_execution,
        "_quiesce_and_finalize_workflow_context",
        failed_cleanup,
    )

    async def fn(ctx, args):
        raise ValueError("workflow failed first")

    with pytest.raises(RuntimeError, match="technical workflow cleanup failed") as caught:
        await workflow_runtime.run_workflow(fn, {}, cfg=_cfg())

    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value.__cause__) == "workflow failed first"

@pytest.mark.asyncio
async def test_run_workflow_cleanup_failure_is_note_on_primary_cancel(monkeypatch):
    _patch_build_session(monkeypatch)
    started = asyncio.Event()

    async def failed_cleanup(ctx, *, timeout):
        return True, False, ()

    monkeypatch.setattr(
        workflow_execution,
        "_quiesce_and_finalize_workflow_context",
        failed_cleanup,
    )

    async def fn(ctx, args):
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(workflow_runtime.run_workflow(fn, {}, cfg=_cfg()))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    run_task.cancel("primary cancellation")

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(run_task, timeout=0.5)
    assert_cancel_reason(caught.value, "primary cancellation")
    assert_cancel_note(
        caught.value,
        "workflow cleanup also failed",
    )

@pytest.mark.asyncio
async def test_run_workflow_reports_manifest_failure(
    monkeypatch,
    tmp_path,
):
    _patch_build_session(monkeypatch)
    class RecordingTracer:
        write_error = None
        dropped_steps = 0

        def __init__(self, *args, **kwargs):
            self.closed = False

        def log_step(self, *args, **kwargs):
            return None

        def close(self):
            self.closed = True

    def failing_manifest(*args, **kwargs):
        raise OSError("manifest disk failed")

    monkeypatch.setattr(workflow_execution, "Tracer", RecordingTracer)
    monkeypatch.setattr(
        workflow_cleanup,
        "_write_workflow_manifest",
        failing_manifest,
    )

    async def fn(ctx, args):
        return "computed"

    with pytest.raises(RuntimeError, match="technical workflow manifest persistence failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.2,
        )

@pytest.mark.asyncio
async def test_run_workflow_waits_for_orphaned_background_agent(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class BackgroundSession:
        used_tokens = 0

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            started.set()
            await release.wait()
            return "background-finished"

    class Factory:
        def build_workflow_session(self, **kwargs):
            return BackgroundSession()

    def fake_build_context(**kwargs):
        return WorkflowContext(
            Factory(),
            max_concurrency=kwargs["max_concurrency"],
            task_concurrency=kwargs["task_concurrency"],
            budget_total=kwargs["budget"],
        )

    monkeypatch.setattr(workflow_execution, "build_workflow_context", fake_build_context)

    async def fn(ctx, args):
        asyncio.create_task(ctx.agent("background"))
        return "workflow-returned"

    run_task = asyncio.create_task(
        workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            cleanup_timeout=0.2,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert run_task.done() is False

    release.set()
    assert await asyncio.wait_for(run_task, timeout=0.5) == "workflow-returned"

@pytest.mark.asyncio
async def test_owned_tracer_closes_when_cleanup_fails(
    monkeypatch,
    tmp_path,
):
    holder: dict[str, Any] = {}
    tracer_instances: list[Any] = []
    order: list[str] = []

    class RecordingTracer:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            tracer_instances.append(self)

        def log_step(self, step_type, payload, tokens=0, latency=0.0) -> None:
            return None

        def close(self) -> None:
            self.closed = True
            order.append("tracer-close")

    class Environment:
        def __init__(self) -> None:
            self._aborted = False

        async def abort(self) -> None:
            self._aborted = True

    class PermanentlyCancellationResistantSession:
        used_tokens = 0

        def __init__(self, environment, tracer) -> None:
            self.env = environment
            self.tracer = tracer
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def add_user_message(self, content: str) -> None:
            return None

        async def run_loop(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                order.append("session-finished")
                return "released-late"

    class Factory:
        def __init__(self, session) -> None:
            self.session = session

        def build_workflow_session(self, **kwargs):
            return self.session

    def fake_build_context(**kwargs):
        environment = Environment()
        session = PermanentlyCancellationResistantSession(
            environment,
            kwargs["tracer"],
        )
        holder.update(environment=environment, session=session)
        context = WorkflowContext(
            Factory(session),
            tracer=kwargs["tracer"],
            max_concurrency=kwargs["max_concurrency"],
            task_concurrency=kwargs["task_concurrency"],
            budget_total=kwargs["budget"],
        )
        holder["context"] = context
        return context

    monkeypatch.setattr(workflow_execution, "Tracer", RecordingTracer)
    monkeypatch.setattr(workflow_execution, "build_workflow_context", fake_build_context)

    async def fn(ctx, args):
        assert await ctx.agent("slow", timeout=0.001) is None
        await asyncio.wait_for(holder["session"].cancel_seen.wait(), timeout=0.5)
        return "workflow-returned"

    with pytest.raises(RuntimeError, match="technical workflow cleanup failed"):
        await workflow_runtime.run_workflow(
            fn,
            {},
            cfg=_cfg(),
            save_dir=str(tmp_path / "run"),
            cleanup_timeout=0.005,
        )

    tracer = tracer_instances[0]
    assert tracer.closed is True
    pending = holder["context"].pending_cleanup_tasks
    holder["session"].release.set()
    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=0.5)
    assert order.index("tracer-close") < order.index("session-finished")
