"""Characterization tests for Scheduler event emission.

Locks the event sequence emitted by Scheduler.spawn and
Scheduler.spawn_with_review. After refactoring the spawn/review lifecycle
flows through SchedulerEvent rather than synthetic SessionEvent tool_* events.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.session_run import GenerationTimeoutError
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent
from opencollab.domain.scheduler import ReviewVerdict
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


async def _spawn_and_settle(scheduler, *args, **kwargs):
    """Spawn and await the resulting background task within one event loop.

    ``Scheduler.spawn`` returns after creating a detached ``_drive_agent`` task;
    awaiting that task in a *second* ``asyncio.run`` would bind it to a different
    loop (``ValueError: future belongs to a different loop``). Doing both in one
    coroutine keeps spawn and the await on the same loop.
    """
    aid = await scheduler.spawn(*args, **kwargs)
    task = scheduler._tasks.get(aid)
    if task is not None:
        await asyncio.wait_for(task, timeout=1.0)
    return aid


class _FakeTeammateSession:
    """Minimal session stand-in: records messages and returns a canned result."""

    def __init__(self, result: str, tokens: int = 0, role: str = "teammate"):
        self._result = result
        self.used_tokens = tokens
        self.added: list[str] = []
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return self._result


class _FakeLeadSession:
    """Stand-in for the Lead session; never run in these tests."""

    def __init__(self):
        self.used_tokens = 0
        self.env = None
        self.agent = type("_Agent", (), {"name": "lead"})()
        self.tool_execution = type("_TP", (), {"safety_policy": None, "env": None})()
        self.runner = type("_R", (), {"max_steps": 100})()
        self.max_steps = 100
        self.state = SessionState(messages=[])
        self.auto_save_path = None

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({"messages": self.state.enriched_messages()}, f)

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _FakeSessionFactory:
    """Drives spawn()/spawn_with_review() with canned teammate sessions."""

    def __init__(self, role_results: dict[str, list[str]]):
        # Map of role -> queue of canned results.
        self._queues = {role: list(results) for role, results in role_results.items()}
        self.built: list[tuple[str, int]] = []
        self.built_tasks: list[tuple[str, str]] = []
        self.built_contexts: list[tuple[str, str]] = []

    def build_lead_session(self, **kwargs):
        return _FakeLeadSession()

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        self.built.append((role, budget))
        self.built_tasks.append((role, task or ""))
        self.built_contexts.append((role, context))
        queue = self._queues.get(role, [])
        result = queue.pop(0) if queue else ""
        return _FakeTeammateSession(result, role=role)


def _build_scheduler(monkeypatch, role_results: dict[str, list[str]]) -> tuple[Scheduler, list[Any]]:
    captured: list[Any] = []

    async def sink(event):
        captured.append(event)

    factory = _FakeSessionFactory(role_results)
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
    )
    lead_session = _FakeLeadSession()
    scheduler.register_lead(lead_session)
    return scheduler, captured


def _scheduler_events(events: list[Any]) -> list[SchedulerEvent]:
    return [e for e in events if isinstance(e, SchedulerEvent)]


def test_cleanup_closes_session_resources_once():
    class ClosableLead(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    lead = ClosableLead()
    scheduler.register_lead(lead)

    run(scheduler.cleanup())

    assert lead.close_calls == 1

def test_cleanup_drains_queued_autosaves_before_final_snapshot(tmp_path):
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    save_path = str(tmp_path / "agent_0_lead.json")
    call_count = 0

    def stale_autosave() -> None:
        nonlocal call_count
        call_count += 1
        current = call_count
        if current == 1:
            first_started.set()
            assert release_first.wait(timeout=2.0)
        else:
            second_started.set()
            assert release_second.wait(timeout=2.0)
        with open(save_path, "w", encoding="utf-8") as handle:
            json.dump({"messages": [{"role": "assistant", "content": f"old-{current}"}]}, handle)

    subscriber = AutoSaveSubscriber(stale_autosave)

    class LeadWithOwnedAutosave(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.auto_save_path = save_path
            self.event_bus = EventBus(subscriber)

        @property
        def pending_cleanup_tasks(self):
            return self.event_bus.pending_tasks

        @property
        def persistence_errors(self):
            return (subscriber.last_error,) if subscriber.last_error else ()

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    lead = LeadWithOwnedAutosave()
    scheduler.register_lead(lead)
    lead.state.append_message({"role": "assistant", "content": "latest state"})

    async def scenario():
        first_waiter = asyncio.create_task(
            lead.event_bus.emit(SessionRuntimeEvent(type="step_end"))
        )
        assert await asyncio.to_thread(first_started.wait, 1.0)
        second_waiter = asyncio.create_task(
            lead.event_bus.emit(SessionRuntimeEvent(type="step_end"))
        )
        await asyncio.gather(first_waiter, second_waiter)
        assert len(subscriber.pending_tasks) == 1

        cleanup = asyncio.create_task(scheduler.cleanup(cleanup_timeout=1.0))
        await asyncio.sleep(0.02)
        assert cleanup.done() is False
        release_first.set()
        assert await asyncio.to_thread(second_started.wait, 0.5)
        assert cleanup.done() is False
        release_second.set()
        await cleanup

    run(scenario())
    with open(save_path, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["messages"][-1]["content"] == "latest state"


def test_cleanup_surfaces_final_session_snapshot_failure(tmp_path):
    error = OSError("final snapshot failed")

    class FailingLead(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.auto_save_path = str(tmp_path / "agent_0_lead.json")

        def save(self, path: str) -> None:
            raise error

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    scheduler.register_lead(FailingLead())

    with pytest.raises(
        RuntimeError,
        match="technical scheduler cleanup failed: session persistence failed",
    ) as caught:
        run(scheduler.cleanup())
    assert caught.value.__cause__ is error


def test_cleanup_reports_nonquiescent_autosave_before_late_release(tmp_path):
    started = threading.Event()
    release = threading.Event()
    save_path = str(tmp_path / "agent_0_lead.json")
    with open(save_path, "w", encoding="utf-8") as handle:
        json.dump({"messages": [{"role": "assistant", "content": "previous"}]}, handle)

    def late_autosave() -> None:
        started.set()
        assert release.wait(timeout=2.0)
        with open(save_path, "w", encoding="utf-8") as handle:
            json.dump({"messages": [{"role": "assistant", "content": "late-old"}]}, handle)

    subscriber = AutoSaveSubscriber(late_autosave)

    class LeadWithLateAutosave(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.auto_save_path = save_path
            self.event_bus = EventBus(subscriber)

        @property
        def pending_cleanup_tasks(self):
            return self.event_bus.pending_tasks

        @property
        def persistence_errors(self):
            return (subscriber.last_error,) if subscriber.last_error else ()

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    lead = LeadWithLateAutosave()
    scheduler.register_lead(lead)
    lead.state.append_message({"role": "assistant", "content": "latest"})

    async def scenario():
        waiter = asyncio.create_task(
            lead.event_bus.emit(SessionRuntimeEvent(type="step_end"))
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        await waiter

        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: session-owned tasks did not quiesce",
        ):
            await scheduler.cleanup(cleanup_timeout=0.01)
        with open(save_path, encoding="utf-8") as handle:
            assert json.load(handle)["messages"][-1]["content"] == "previous"

        pending = subscriber.pending_tasks
        assert pending
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)

    run(scenario())
    with open(save_path, encoding="utf-8") as handle:
        assert json.load(handle)["messages"][-1]["content"] == "late-old"


def test_cleanup_surfaces_sticky_subscriber_persistence_error(tmp_path):
    error = OSError("background snapshot failed")

    def failed_autosave() -> None:
        raise error

    subscriber = AutoSaveSubscriber(failed_autosave)

    class LeadWithFailedAutosave(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.auto_save_path = str(tmp_path / "agent_0_lead.json")
            self.event_bus = EventBus(subscriber)

        @property
        def pending_cleanup_tasks(self):
            return self.event_bus.pending_tasks

        @property
        def persistence_errors(self):
            return (subscriber.last_error,) if subscriber.last_error else ()

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    lead = LeadWithFailedAutosave()
    scheduler.register_lead(lead)

    async def scenario():
        await lead.event_bus.emit(SessionRuntimeEvent(type="step_end"))
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: session persistence failed",
        ) as caught:
            await scheduler.cleanup()
        assert caught.value.__cause__ is error

    run(scenario())


def test_cleanup_bounds_blocking_final_snapshot_and_keeps_owner_visible(tmp_path):
    started = threading.Event()
    release = threading.Event()
    save_path = str(tmp_path / "agent_0_lead.json")

    class BlockingFinalLead(_FakeLeadSession):
        def __init__(self):
            super().__init__()
            self.auto_save_path = save_path

        def save(self, path: str) -> None:
            started.set()
            assert release.wait(timeout=2.0)
            super().save(path)

    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    lead = BlockingFinalLead()
    scheduler.register_lead(lead)
    lead.state.append_message({"role": "assistant", "content": "latest"})

    async def scenario():
        cleanup = asyncio.create_task(scheduler.cleanup(cleanup_timeout=0.01))
        assert await asyncio.to_thread(started.wait, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.02)
        assert cleanup.done() is False
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: session-owned tasks did not quiesce",
        ):
            await cleanup
        pending = scheduler._fallback_autosavers[0].pending_tasks
        assert pending
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)

    run(scenario())
    with open(save_path, encoding="utf-8") as handle:
        assert json.load(handle)["messages"][-1]["content"] == "latest"


def test_cleanup_surfaces_sticky_manifest_failure_after_later_success():
    scheduler, _ = _build_scheduler(None, {})
    error = OSError("first manifest failed")
    calls = 0

    def flaky_manifest() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error

    scheduler.set_manifest_writer(flaky_manifest)
    assert scheduler._write_manifest() is error
    assert scheduler._write_manifest() is None

    with pytest.raises(
        RuntimeError,
        match="technical scheduler cleanup failed: session persistence failed",
    ) as caught:
        run(scheduler.cleanup())
    assert caught.value.__cause__ is error


def test_cleanup_surfaces_final_manifest_failure():
    scheduler, _ = _build_scheduler(None, {})
    error = OSError("final manifest failed")

    def failed_manifest() -> None:
        raise error

    scheduler.set_manifest_writer(failed_manifest)
    with pytest.raises(
        RuntimeError,
        match="technical scheduler cleanup failed: session persistence failed",
    ) as caught:
        run(scheduler.cleanup())
    assert caught.value.__cause__ is error


def test_cleanup_bounds_blocking_final_manifest_and_keeps_owner_visible():
    scheduler, _ = _build_scheduler(None, {})
    started = threading.Event()
    release = threading.Event()

    def blocking_manifest() -> None:
        started.set()
        assert release.wait(timeout=2.0)

    scheduler.set_manifest_writer(blocking_manifest)

    async def scenario():
        cleanup = asyncio.create_task(scheduler.cleanup(cleanup_timeout=0.01))
        assert await asyncio.to_thread(started.wait, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.02)
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: session-owned tasks did not quiesce",
        ):
            await cleanup
        assert scheduler._manifest_subscriber is not None
        pending = scheduler._manifest_subscriber.pending_tasks
        assert pending
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)

    run(scenario())


def test_team_cleanup_tracks_abandoned_provider_task_until_exit():
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
            name="lead",
            system_prompt="sys",
            tools=[],
            model="test-model",
            provider="test",
        ),
        llm=llm,
        llm_timeout=0.01,
    )
    scheduler = Scheduler(
        session_factory=_FakeSessionFactory({}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(),
    )
    scheduler.register_lead(session)

    async def scenario():
        await session.add_user_message("trigger provider timeout")
        with pytest.raises(GenerationTimeoutError):
            await session.run_loop()
        await asyncio.wait_for(llm.cancel_seen.wait(), timeout=0.5)
        assert session.pending_cleanup_tasks
        terminal_reason = session.state.terminal_reason

        pending = session.pending_cleanup_tasks
        try:
            with pytest.raises(
                RuntimeError,
                match="technical scheduler cleanup failed: session-owned tasks did not quiesce",
            ):
                await scheduler.cleanup(cleanup_timeout=0.01)
            assert pending
            assert session.state.phase is SessionPhase.ERROR
            assert session.state.terminal_reason == terminal_reason
        finally:
            llm.release.set()
            await asyncio.gather(*pending, return_exceptions=True)
        assert session.pending_cleanup_tasks == ()
        assert session.state.phase is SessionPhase.ERROR
        assert session.state.terminal_reason == terminal_reason

    run(scenario())


def test_spawn_with_review_emits_review_lifecycle_around_spawns(monkeypatch):
    scheduler, events = _build_scheduler(
        monkeypatch,
        {
            "coder": ["implemented X"],
            "reviewer": ["Looks good.\nVERDICT: PASS"],
        },
    )

    result = run(scheduler.spawn_with_review(0, "write a function", max_iterations=3))

    assert "PASSED after 1 iteration" in result
    assert "implemented X" in result

    seq = _scheduler_events(events)
    # review_started → coder spawn → agent_completed → reviewer spawn → agent_completed → review_completed
    types = [e.type for e in seq]
    assert "review_started" in types
    assert "review_completed" in types
    assert "agent_spawned" in types
    assert "agent_completed" in types


def test_spawn_with_review_iterates_when_reviewer_fails(monkeypatch):
    scheduler, events = _build_scheduler(
        monkeypatch,
        {
            "coder": ["v1 impl", "v2 impl"],
            "reviewer": [
                "Issues found.\nVERDICT: FAIL",
                "All good.\nVERDICT: PASS",
            ],
        },
    )

    result = run(scheduler.spawn_with_review(0, "write fn", max_iterations=3))

    assert "PASSED after 2 iteration" in result
    assert "v2 impl" in result

    seq = _scheduler_events(events)
    review_loops = [e for e in seq if e.type in ("review_started", "review_completed")]
    # 2 iterations × (review_started + review_completed)
    assert [e.type for e in review_loops] == [
        "review_started",
        "review_completed",
        "review_started",
        "review_completed",
    ]
    assert review_loops[0].data["iteration"] == 1
    assert review_loops[1].data["verdict"] == "FAIL"
    assert review_loops[2].data["iteration"] == 2
    assert review_loops[3].data["verdict"] == "PASS"

    factory = scheduler._session_factory
    second_coder_task = [task for role, task in factory.built_tasks if role == "coder"][1]
    assert "v1 impl" in second_coder_task
    assert "Reapply the previous implementation" in second_coder_task


def test_spawn_with_review_retains_final_reviewer_feedback(monkeypatch):
    scheduler, _ = _build_scheduler(
        monkeypatch,
        {
            "coder": ["final implementation"],
            "reviewer": ["Critical race remains.\nVERDICT: FAIL"],
        },
    )

    result = run(scheduler.spawn_with_review(0, "write fn", max_iterations=1))

    assert "final implementation" in result
    assert "Last reviewer feedback:\nCritical race remains.\nVERDICT: FAIL" in result


def test_spawn_with_review_returns_artifact_when_reviewer_spawn_fails(monkeypatch):
    scheduler, events = _build_scheduler(
        monkeypatch,
        {"coder": ["recoverable implementation"]},
    )
    real_spawn = scheduler.spawn

    async def fail_reviewer(parent_aid, role, task, context="", **kwargs):
        if role == "reviewer":
            raise RuntimeError("reviewer infrastructure unavailable")
        return await real_spawn(parent_aid, role, task, context, **kwargs)

    monkeypatch.setattr(scheduler, "spawn", fail_reviewer)

    result = run(scheduler.spawn_with_review(0, "write fn", max_iterations=1))

    assert "recoverable implementation" in result
    assert "Failure stage: reviewer_spawn" in result
    assert "reviewer infrastructure unavailable" in result
    review_events = [
        event.type
        for event in _scheduler_events(events)
        if event.type.startswith("review_")
    ]
    assert review_events == ["review_started", "review_completed"]


@pytest.mark.parametrize("failure_mode", ["wait", "terminal"])
def test_spawn_with_review_retains_prior_artifact_when_next_coder_is_empty(
    monkeypatch,
    failure_mode,
):
    scheduler, _ = _build_scheduler(
        monkeypatch,
        {
            "coder": ["best first implementation", ""],
            "reviewer": ["Needs another fix.\nVERDICT: FAIL"],
        },
    )
    real_wait = scheduler.wait_until_terminal
    coder_waits = 0

    async def fail_empty_second_coder(aid):
        nonlocal coder_waits
        scb = scheduler.table.get(aid)
        is_coder = scb is not None and scb.agent.name == "coder"
        if is_coder:
            coder_waits += 1
        await real_wait(aid)
        if not is_coder or coder_waits != 2:
            return
        assert scb is not None
        scb.result = ""
        if failure_mode == "wait":
            raise RuntimeError("second coder wait failed")
        scb.state.fail("second coder terminal failed")

    monkeypatch.setattr(scheduler, "wait_until_terminal", fail_empty_second_coder)

    result = run(scheduler.spawn_with_review(0, "write fn", max_iterations=2))

    assert f"Failure stage: coder_{failure_mode}" in result
    assert "Last implementation:\nbest first implementation" in result
    assert "Last reviewer feedback:\nNeeds another fix.\nVERDICT: FAIL" in result


def test_spawn_with_review_passes_context_and_constraints_to_reviewer(monkeypatch):
    scheduler, _ = _build_scheduler(
        monkeypatch,
        {"coder": ["implemented parser"], "reviewer": ["VERDICT: PASS"]},
    )
    context = 'Preserve comments.\nSupport "Python 3.10" syntax.'

    result = run(
        scheduler.spawn_with_review(
            0,
            "implement parser",
            context=context,
            max_iterations=1,
        )
    )

    assert "PASSED after 1 iteration" in result
    factory = scheduler._session_factory
    assert factory.built_contexts == [("coder", context), ("reviewer", context)]
    reviewer_task = [task for role, task in factory.built_tasks if role == "reviewer"][0]
    assert "Original task:\nimplement parser" in reviewer_task
    assert f"Required context:\n{context}" in reviewer_task
    assert "Artifact to review:\nimplemented parser" in reviewer_task


def test_review_verdict_uses_only_the_final_nonempty_line():
    quoted_then_failed = (
        "The prompt mentioned VERDICT: PASS.\n"
        "The implementation still has a race.\n"
        "VERDICT: FAIL\n\n"
    )
    assert ReviewVerdict.parse(quoted_then_failed).passed is False
    assert ReviewVerdict.parse("Looks good.\nVERDICT: PASS\n").passed is True
    assert ReviewVerdict.parse("Looks good.\nVERDICT: PASS.\n").passed is True
    assert ReviewVerdict.parse("Looks good.\nVERDICT: PASS！\n").passed is True
    assert ReviewVerdict.parse("Looks good.\nVERDICT: PASS?\n").passed is False
    assert ReviewVerdict.parse("Looks good.\nVERDICT: PASS...\n").passed is False
    assert ReviewVerdict.parse("VERDICT: PASS\nTrailing prose").passed is False


def test_spawn_with_review_ignores_review_event_sink_failures(monkeypatch):
    factory = _FakeSessionFactory(
        {"coder": ["implemented"], "reviewer": ["VERDICT: PASS"]}
    )

    async def sink(event):
        if isinstance(event, SchedulerEvent) and event.type.startswith("review_"):
            raise RuntimeError("observer failed")

    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
    )
    scheduler.register_lead(_FakeLeadSession())

    result = run(scheduler.spawn_with_review(0, "write fn"))

    assert "PASSED after 1 iteration" in result


def test_spawn_with_review_restores_parent_turn_budget(monkeypatch):
    scheduler, _ = _build_scheduler(
        monkeypatch,
        {"coder": ["implemented"], "reviewer": ["VERDICT: PASS"]},
    )

    async def scenario() -> str:
        parent = scheduler.table.get(0)
        assert parent is not None
        parent.state.set_phase(SessionPhase.EXECUTING_TOOLS)
        scheduler._tasks[0] = asyncio.current_task()
        try:
            result = await scheduler.spawn_with_review(0, "write fn")
            assert 0 in scheduler._turn_lease
            assert scheduler.allocated_tokens <= scheduler._max_budget_tokens
            return result
        finally:
            scheduler._tasks.pop(0, None)

    assert "PASSED after 1 iteration" in run(scenario())


def test_external_spawn_with_review_preserves_seed_for_later_spawn(monkeypatch):
    scheduler, _ = _build_scheduler(
        monkeypatch,
        {
            "coder": ["implemented", "follow-up complete"],
            "reviewer": ["VERDICT: PASS"],
        },
    )

    async def scenario() -> None:
        result = await scheduler.spawn_with_review(0, "write fn")
        assert "PASSED after 1 iteration" in result
        aid = await scheduler.spawn(0, "coder", "later independent task")
        assert scheduler._session_factory.built[-1][1] > 0
        await scheduler._tasks[aid]

    run(scenario())


def test_spawn_does_not_emit_session_tool_events(monkeypatch):
    """Scheduler.spawn must not re-use session_runtime tool_start/tool_end semantics."""
    scheduler, events = _build_scheduler(monkeypatch, {"coder": ["ok"]})

    run(_spawn_and_settle(scheduler, 0, "coder", "x"))

    # Every event from scheduler orchestration must be a SchedulerEvent now.
    types = [getattr(e, "type", None) for e in events]
    assert "tool_start" not in types
    assert "tool_end" not in types
