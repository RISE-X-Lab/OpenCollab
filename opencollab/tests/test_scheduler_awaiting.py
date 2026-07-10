"""Scheduler-side tests for event-driven suspend/resume.

A ``ScriptedSession`` stands in for a real Session, honoring the contract the
scheduler relies on: ``run_loop`` returns with the session either suspended on
``AWAITING_EVENTS`` (after recording pending rows) or terminal. On resume the
scheduler has filled the pending table; the scripted session drains it and
finishes. This exercises the scheduler's origin map, ``_wake``, ``_quiescent``,
and ``run`` quiescence without standing up an LLM.
"""

from __future__ import annotations

import asyncio

import pytest
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.pending import PendingRow, PendingRowError, RowKind, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


class ScriptedSession:
    """run_loop pops the next scripted step; each step mutates state + returns."""

    def __init__(self, role, steps):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.added: list[str] = []
        self._steps = list(steps)
        self.scheduler: Scheduler | None = None

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        step = self._steps.pop(0)
        return await step(self)


def suspend_spawning(specs):
    """Build a step that spawns one child per spec and suspends.

    ``specs`` is a list of (role, task, tool_call_id). Adds a PENDING row per
    spawn referencing the child aid, then transitions to AWAITING_EVENTS.
    """

    async def step(sess: ScriptedSession) -> str:
        for order, (role, task, tcid) in enumerate(specs):
            child_aid = await sess.scheduler.spawn(
                sess.state.aid, role, task, tool_call_id=tcid
            )
            sess.state.pending_events.add(
                PendingRow(
                    tool_call_id=tcid,
                    kind=RowKind.CHILD_AGENT,
                    order=order,
                    ref=child_aid,
                    status=RowStatus.PENDING,
                )
            )
        sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
        sess.state.append_message({"role": "assistant", "content": "delegating"})
        return "delegating"

    return step


def resume_done(final_builder):
    """Build a step that drains a now-complete pending table and finishes."""

    async def step(sess: ScriptedSession) -> str:
        assert sess.state.pending_events.is_complete()
        results = [r.result for r in sorted(sess.state.pending_events.rows.values(), key=lambda r: r.order)]
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        final = final_builder(results)
        sess.state.append_message({"role": "assistant", "content": final})
        return final

    return step


def terminal(result):
    async def step(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": result})
        return result

    return step


class ScriptedFactory:
    """Hands out pre-scripted child sessions in spawn order, assigning aid."""

    def __init__(self, children, scheduler_ref):
        self._children = list(children)
        self._scheduler_ref = scheduler_ref

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        sess = self._children.pop(0)
        sess.state.aid = aid
        sess.scheduler = scheduler
        return sess


def build_scheduler(lead, children):
    captured: list = []

    async def sink(event):
        captured.append(event)

    holder: dict = {}
    factory = ScriptedFactory(children, holder)
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
    )
    holder["scheduler"] = scheduler
    lead.scheduler = scheduler
    scheduler.register_lead(lead)
    return scheduler, captured


def event_types(events, type_):
    return [e for e in events if isinstance(e, SchedulerEvent) and e.type == type_]


def test_single_deferred_spawn_round_trip():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [terminal("child output")])
    scheduler, events = build_scheduler(lead, [child])

    result = run(scheduler.run("please delegate"))

    # The lead reasoned over the child's result in the SAME turn — the gap closed.
    assert result == "final: child output"
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert len(event_types(events, "agent_resumed")) == 1
    assert len(event_types(events, "agent_completed")) == 2  # child + lead


def test_multiple_parallel_children_resume_exactly_once():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning(
                [("coder", "a", "tc-1"), ("coder", "b", "tc-2"), ("coder", "c", "tc-3")]
            ),
            resume_done(lambda results: "merged: " + ",".join(results)),
        ],
    )
    children = [
        ScriptedSession("coder", [terminal("ra")]),
        ScriptedSession("coder", [terminal("rb")]),
        ScriptedSession("coder", [terminal("rc")]),
    ]
    scheduler, events = build_scheduler(lead, children)

    result = run(scheduler.run("delegate three"))

    assert result == "merged: ra,rb,rc"
    # The last child to finish triggers exactly one resume — no double-wake.
    assert len(event_types(events, "agent_resumed")) == 1
    assert lead.state.pending_events.is_empty()


def test_last_child_completion_in_parent_return_tail_still_resumes():
    lead = ScriptedSession(
        "lead",
        [resume_done(lambda results: f"resumed: {results[0]}")],
    )
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(
            tool_call_id="tail-child",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=1,
        )
    )

    async def scenario():
        release_parent = asyncio.Event()

        async def parent_tail():
            await release_parent.wait()

        original = asyncio.create_task(parent_tail())
        scheduler._tasks[0] = original
        await scheduler._wake(0, "tail-child", "done", RowStatus.DONE)
        assert scheduler._tasks[0] is original
        assert lead.state.pending_events.is_complete()

        release_parent.set()
        await original
        for _ in range(20):
            replacement = scheduler._tasks.get(0)
            if replacement is not None and replacement is not original:
                await replacement
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("parent resume task was never scheduled")

    run(scenario())

    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert scheduler.table.get(0).result == "resumed: done"
    assert len(event_types(events, "agent_resumed")) == 1


def test_nested_delegation_resumes_up_the_tree():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("manager", "plan", "L1")]),
            resume_done(lambda results: f"lead<{results[0]}>"),
        ],
    )
    manager = ScriptedSession(
        "manager",
        [
            suspend_spawning([("coder", "impl", "M1")]),
            resume_done(lambda results: f"mgr<{results[0]}>"),
        ],
    )
    grandchild = ScriptedSession("coder", [terminal("leaf")])
    scheduler, events = build_scheduler(lead, [manager, grandchild])

    result = run(scheduler.run("deep delegate"))

    assert result == "lead<mgr<leaf>>"
    assert len(event_types(events, "agent_resumed")) == 2  # manager, then lead


def test_spawn_with_review_waits_for_nested_coder_terminal_result():
    lead = ScriptedSession("lead", [])
    coder = ScriptedSession(
        "coder",
        [
            suspend_spawning([("analyst", "inspect", "nested-1")]),
            resume_done(lambda results: f"final implementation from {results[0]}"),
        ],
    )
    analyst = ScriptedSession("analyst", [terminal("analysis")])
    reviewer = ScriptedSession("reviewer", [terminal("VERDICT: PASS")])
    scheduler, _ = build_scheduler(lead, [coder, analyst, reviewer])

    result = run(
        asyncio.wait_for(
            scheduler.spawn_with_review(0, "implement nested task", max_iterations=1),
            timeout=1,
        )
    )

    assert "PASSED after 1 iteration" in result
    assert "final implementation from analysis" in result
    assert "delegating" not in result


def test_wake_unknown_tool_call_id_raises():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )

    with pytest.raises(PendingRowError):
        run(scheduler._wake(0, "tc-unknown", "x", RowStatus.DONE))


def test_misrouted_completion_emits_failure_not_silent():
    lead = ScriptedSession("lead", [])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )
    # Register a bogus origin pointing at a tool_call_id that doesn't exist.
    scheduler._spawn_origin[99] = (0, "tc-bogus")

    run(scheduler._deliver_to_parent(99, "result", RowStatus.DONE))

    assert len(event_types(events, "agent_failed")) == 1


def test_send_message_queues_when_target_awaiting_events():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession("coder", [])
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    result = run(scheduler.send_message(0, 1, "question", "are you there?"))

    assert result == "Message queued to aid 1."
    assert target.added == []  # never delivered
    assert scheduler._message_inbox[1][0].content == "are you there?"


def test_queued_message_delivers_after_target_awaiting_events_resumes():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession(
        "coder",
        [
            resume_done(lambda results: f"finished {results[0]}"),
            terminal("message handled"),
        ],
    )
    target.state.aid = 1
    target.scheduler = scheduler
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    target.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=7)
    )
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    async def scenario():
        await scheduler.send_message(0, 1, "follow up", "are you there?")
        await scheduler._wake(1, "tc-1", "child done", RowStatus.DONE)
        for _ in range(3):
            task = scheduler._tasks.get(1)
            if task is None:
                break
            await task
            if scheduler._tasks.get(1) is task:
                break

    run(scenario())

    assert target.added == [
        '<teammate-message teammate_id="A0" summary="follow up">\n'
        "are you there?\n"
        "</teammate-message>"
    ]
    assert scheduler.table.get(1).result == "message handled"


def test_child_message_to_suspended_parent_does_not_stall_turn():
    async def child_reports_progress(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(
            sess.state.aid,
            0,
            "progress",
            "analysis started",
        )
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "child final"})
        return "child final"

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("analyst", "investigate", "tc-1")]),
            resume_done(lambda results: f"lead saw {results[0]}"),
            terminal("progress message handled"),
        ],
    )
    child = ScriptedSession("analyst", [child_reports_progress])
    scheduler, events = build_scheduler(lead, [child])

    result = run(asyncio.wait_for(scheduler.run("go"), timeout=1))

    assert result == "progress message handled"
    assert scheduler._message_inbox.get(0) == []
    assert lead.added[-1] == (
        '<teammate-message teammate_id="A1" summary="progress">\n'
        "analysis started\n"
        "</teammate-message>"
    )
    assert len(event_types(events, "agent_message_sent")) == 1
    assert len(event_types(events, "agent_message_delivered")) == 1


def test_scheduler_run_retrieves_finished_task_exception(caplog):
    lead = ScriptedSession("lead", [terminal("done")])
    scheduler, _ = build_scheduler(lead, [])

    class FinishedTask:
        result_called = False

        def done(self):
            return True

        def result(self):
            self.result_called = True
            raise RuntimeError("background failure")

    finished = FinishedTask()
    scheduler._tasks[99] = finished

    assert run(scheduler.run("go")) == "done"
    assert finished.result_called is True
    assert "background task for aid 99 failed" in caplog.text


def test_scheduler_run_does_not_return_previous_turn_answer():
    async def precheck_stop(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.STEP_LIMIT_EXCEEDED)
        sess.state.append_message(
            {"role": "system", "content": "[Step limit reached. Session stopped.]"}
        )
        return ""

    lead = ScriptedSession("lead", [precheck_stop])
    lead.state.append_message({"role": "assistant", "content": "previous answer"})
    scheduler, _ = build_scheduler(lead, [])

    assert run(asyncio.wait_for(scheduler.run("new question"), timeout=0.5)) == ""


def test_concurrent_scheduler_runs_are_serialized_on_the_lead_session():
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def first_turn(sess: ScriptedSession) -> str:
        first_started.set()
        await first_release.wait()
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "first answer"})
        return "first answer"

    lead = ScriptedSession(
        "lead",
        [first_turn, terminal("second answer")],
    )
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        first_call = asyncio.create_task(scheduler.run("first question"))
        await first_started.wait()
        first_driver = scheduler._tasks[0]
        second_call = asyncio.create_task(scheduler.run("second question"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert lead.added == ["first question"]
        assert scheduler._tasks[0] is first_driver
        assert second_call.done() is False

        first_release.set()
        assert await asyncio.wait_for(first_call, timeout=0.5) == "first answer"
        assert await asyncio.wait_for(second_call, timeout=0.5) == "second answer"
        assert lead.added == ["first question", "second question"]

    run(scenario())


def test_scheduler_run_cancellation_tears_down_owned_team_before_propagating():
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_turn(sess: ScriptedSession) -> str:
        started.set()
        await never_release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [blocking_turn])
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        run_task = asyncio.create_task(scheduler.run("cancel this turn"))
        await started.wait()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=0.5)

        assert scheduler._shutting_down is True
        assert scheduler._tasks == {}
        assert scheduler.table.get(0).state.phase is SessionPhase.CANCELLED
        assert scheduler._lead_reservation is None

    run(scenario())


def test_cleanup_rolls_back_forever_lead_add_and_blocks_late_driver():
    class StubbornLead(ScriptedSession):
        def __init__(self):
            super().__init__("lead", [terminal("must never run")])
            self.add_started = asyncio.Event()
            self.add_release = asyncio.Event()
            self.add_finished = asyncio.Event()
            self.cancellations = 0

        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            self.add_started.set()
            while not self.add_release.is_set():
                try:
                    await self.add_release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.state.append_message(
                {"role": "user", "content": "late lead mutation"}
            )
            self.add_finished.set()

    lead = StubbornLead()
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        run_task = asyncio.create_task(scheduler.run("blocked lead add"))
        await lead.add_started.wait()
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )
        with pytest.raises(asyncio.CancelledError):
            await run_task

        assert lead.cancellations >= 2
        assert scheduler._active_run_tasks == set()
        assert scheduler._lead_turn_record is None
        assert scheduler._tasks == {}
        assert scheduler.table.get(0).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(0).state.messages == []
        assert scheduler._lead_reservation is None

        lead.add_release.set()
        await asyncio.wait_for(lead.add_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scheduler._tasks == {}
        assert scheduler.table.get(0).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(0).state.messages == []
        assert scheduler.table.get(0).result.startswith("Error: scheduler cleanup")

    run(scenario())


def test_running_cancelled_child_fails_parent_row_and_resumes_parent():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        return "unreachable"

    lead = ScriptedSession(
        "lead", [resume_done(lambda results: f"resumed with {results[0]}")]
    )
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(0, "coder", "cancel me", tool_call_id="cancel-me")
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="cancel-me",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        await started.wait()
        scheduler._tasks[aid].cancel()
        with pytest.raises(asyncio.CancelledError):
            await scheduler._tasks[aid]
        await scheduler.wait_until_terminal(0)
        return aid

    aid = run(scenario())

    assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
    assert scheduler.table.get(aid).result.startswith("Error: agent cancelled")
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert "Error: agent cancelled" in scheduler.table.get(0).result
    assert aid not in scheduler._spawn_origin


def test_cancelled_spawn_before_driver_fails_parent_row_without_ghost_child():
    class BlockingPool:
        def __init__(self):
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def acquire(self, role):
            self.started.set()
            await self.gate.wait()

        async def release(self):
            return None

    lead = ScriptedSession(
        "lead", [resume_done(lambda results: f"startup failed: {results[0]}")]
    )
    scheduler, _ = build_scheduler(lead, [])
    pool = BlockingPool()
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="startup-cancel",
                kind=RowKind.CHILD_AGENT,
                order=0,
            )
        )
        task = asyncio.create_task(
            scheduler.spawn(
                0,
                "coder",
                "cancel during acquire",
                tool_call_id="startup-cancel",
            )
        )
        await pool.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert set(scheduler.table.entries) == {0}
    assert set(scheduler._sessions) == {0}
    assert scheduler._spawn_origin == {}
    assert lead.state.pending_events.is_empty()
    assert "spawn cancelled before startup" in scheduler.table.get(0).result


def test_cleanup_cancellation_does_not_start_message_replacement_task():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child, terminal("message handled")])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "long task")
        await started.wait()
        await scheduler.send_message(0, aid, "queued", "do this next")
        await scheduler.cleanup()
        return aid

    aid = run(scenario())

    assert scheduler._shutting_down is True
    assert scheduler._tasks == {}
    assert child.state.phase is SessionPhase.CANCELLED
    assert scheduler._message_inbox[aid]


def test_cleanup_finalizes_driver_cancelled_before_first_timeslice():
    never_release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        await never_release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "cancel immediately")
        assert aid in scheduler._child_reservation
        await scheduler.cleanup()
        return aid

    aid = run(scenario())

    assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
    assert aid not in scheduler._child_reservation
    assert scheduler.inflight_spawn("coder", "cancel immediately") is None
    assert aid not in scheduler._spawn_origin


def test_cleanup_releases_seeded_lead_lease_without_an_active_turn():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    assert scheduler._lead_reservation is not None
    assert scheduler.allocated_tokens > 0

    run(scheduler.cleanup(cleanup_timeout=0.01))

    assert scheduler._lead_reservation is None
    assert scheduler.allocated_tokens == 0


def test_cleanup_is_bounded_when_session_ignores_both_cancellations():
    class AbortTrackingEnv:
        def __init__(self):
            self.aborted = asyncio.Event()

        async def abort(self):
            self.aborted.set()

    class RecordingPool:
        def __init__(self, env):
            self.env = env
            self.released = asyncio.Event()

        async def acquire(self, role):
            return self.env

        async def release(self):
            self.released.set()

    class StubbornSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()
            self.cancellations = 0

        async def run_loop(self):
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.finished.set()
            return "late success must be discarded"

    env = AbortTrackingEnv()
    pool = RecordingPool(env)
    child = StubbornSession(env)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "ignore cancellation",
            tool_call_id="stubborn-child",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="stubborn-child",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        await child.started.wait()
        driver = scheduler._tasks[aid]
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )

        row = lead.state.pending_events.rows["stubborn-child"]
        assert env.aborted.is_set()
        assert pool.released.is_set() is False
        assert child.cancellations >= 2
        assert row.status is RowStatus.FAILED
        assert row.error and "scheduler cleanup" in row.error
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")
        assert aid not in scheduler._child_reservation
        assert scheduler.inflight_spawn("coder", "ignore cancellation") is None
        assert scheduler._tasks == {}

        child.release.set()
        await asyncio.wait_for(child.finished.wait(), timeout=0.5)
        await asyncio.wait_for(driver, timeout=0.5)
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")

    run(scenario())


def test_cleanup_caller_cancellation_waits_for_owned_teardown_then_propagates():
    class AbortTrackingEnv:
        def __init__(self):
            self.aborted = asyncio.Event()

        async def abort(self):
            self.aborted.set()

    class RecordingPool:
        def __init__(self, env):
            self.env = env
            self.released = asyncio.Event()

        async def acquire(self, role):
            return self.env

        async def release(self):
            self.released.set()

    class CancellationResistantSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def run_loop(self):
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_seen.set()
            return "late"

    env = AbortTrackingEnv()
    pool = RecordingPool(env)
    child = CancellationResistantSession(env)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "cancel cleanup caller",
            tool_call_id="cancel-cleanup",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="cancel-cleanup",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        await child.started.wait()
        driver = scheduler._tasks[aid]
        cleanup_task = asyncio.create_task(
            scheduler.cleanup(cleanup_timeout=0.01)
        )
        await child.cancel_seen.wait()
        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup_task, timeout=0.5)

        row = lead.state.pending_events.rows["cancel-cleanup"]
        assert env.aborted.is_set()
        assert pool.released.is_set() is False
        assert scheduler._tasks == {}
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert row.status is RowStatus.FAILED
        assert aid not in scheduler._child_reservation
        assert scheduler.inflight_spawn("coder", "cancel cleanup caller") is None

        child.release.set()
        await asyncio.wait_for(driver, timeout=0.5)

    run(scenario())


def test_cleanup_wins_race_after_delivery_starts_before_parent_row_fill():
    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [terminal("late success")])
    scheduler, _ = build_scheduler(lead, [child])
    real_wake = scheduler._wake
    wake_entered = asyncio.Event()
    release_wake = asyncio.Event()
    cancellations = 0

    async def gated_wake(*args, **kwargs):
        nonlocal cancellations
        wake_entered.set()
        while not release_wake.is_set():
            try:
                await release_wake.wait()
            except asyncio.CancelledError:
                cancellations += 1
        return await real_wake(*args, **kwargs)

    scheduler._wake = gated_wake

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "finish during cleanup",
            tool_call_id="delivery-race",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="delivery-race",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        driver = scheduler._tasks[aid]
        await wake_entered.wait()
        assert aid in scheduler._spawn_origin

        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )
        row = lead.state.pending_events.rows["delivery-race"]
        assert cancellations >= 2
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")
        assert aid not in scheduler._spawn_origin

        release_wake.set()
        await asyncio.wait_for(driver, timeout=0.5)
        row = lead.state.pending_events.rows["delivery-race"]
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED

    run(scenario())


@pytest.mark.parametrize("blocked_stage", ["diff", "event"])
def test_cleanup_prevents_late_finalization_stage_from_flipping_parent_row(
    blocked_stage,
):
    class BlockingFinalizationEnv:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.aborted = asyncio.Event()
            self.cancellations = 0

        async def get_diff(self):
            if blocked_stage != "diff":
                return ""
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            return "diff --git a/x b/x\n+late\n"

        async def abort(self):
            self.aborted.set()

    class BlockingEventSink:
        def __init__(self, env):
            self.env = env

        async def emit(self, event):
            if blocked_stage != "event" or event.type != "agent_completed":
                return
            self.env.started.set()
            while not self.env.release.is_set():
                try:
                    await self.env.release.wait()
                except asyncio.CancelledError:
                    self.env.cancellations += 1

    env = BlockingFinalizationEnv()
    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [terminal("late success")])
    child.env = env
    scheduler, _ = build_scheduler(lead, [child])
    scheduler._event_sink = BlockingEventSink(env)

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            f"block {blocked_stage}",
            tool_call_id=f"late-{blocked_stage}",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id=f"late-{blocked_stage}",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        driver = scheduler._tasks[aid]
        await env.started.wait()
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )

        row = lead.state.pending_events.rows[f"late-{blocked_stage}"]
        assert env.aborted.is_set()
        assert env.cancellations >= 2
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED

        env.release.set()
        await asyncio.wait_for(driver, timeout=0.5)
        row = lead.state.pending_events.rows[f"late-{blocked_stage}"]
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")

    run(scenario())


def test_cleanup_is_bounded_when_worktree_release_ignores_cancellation():
    class StubbornReleasePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.release_gate = asyncio.Event()
            self.finished = asyncio.Event()
            self.cancellations = 0

        async def acquire(self, role):
            return None

        async def release(self):
            self.started.set()
            while not self.release_gate.is_set():
                try:
                    await self.release_gate.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.finished.set()

    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    pool = StubbornReleasePool()
    scheduler._worktree_pool = pool

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: worktree pool release failed or timed out",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )
        assert pool.started.is_set()
        assert pool.cancellations >= 1
        pool.release_gate.set()
        await asyncio.wait_for(pool.finished.wait(), timeout=0.5)

    run(scenario())


def test_cleanup_surfaces_synchronous_worktree_release_failure():
    error = OSError("pool release failed")

    class FailingReleasePool:
        async def acquire(self, role):
            return None

        def release(self):
            raise error

    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    scheduler._worktree_pool = FailingReleasePool()

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: worktree pool release failed or timed out",
        ) as caught:
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert "worktree pool" in str(caught.value)

    run(scenario())


def test_cleanup_surfaces_environment_abort_timeout_and_late_task_stays_terminal():
    class StubbornAbortEnv:
        def __init__(self):
            self.abort_started = asyncio.Event()
            self.abort_release = asyncio.Event()
            self.abort_finished = asyncio.Event()

        async def abort(self):
            self.abort_started.set()
            while not self.abort_release.is_set():
                try:
                    await self.abort_release.wait()
                except asyncio.CancelledError:
                    continue
            self.abort_finished.set()

    class StubbornSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_loop(self):
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            return "late completion"

    env = StubbornAbortEnv()
    child = StubbornSession(env)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])

    class RecordingPool:
        def __init__(self):
            self.release_called = False

        async def acquire(self, _role):
            return env

        async def release(self):
            self.release_called = True

    pool = RecordingPool()
    scheduler._worktree_pool = pool

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "stay active")
        driver = scheduler._tasks[aid]
        await child.started.wait()
        with pytest.raises(RuntimeError) as caught:
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert "execution tasks did not quiesce" in str(caught.value)
        assert "session environment abort failed or timed out" in str(caught.value)
        assert env.abort_started.is_set()
        assert env._aborted is True
        assert pool.release_called is False
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED

        child.release.set()
        env.abort_release.set()
        await asyncio.wait_for(driver, timeout=0.5)
        await asyncio.wait_for(env.abort_finished.wait(), timeout=0.5)
        assert scheduler.table.get(aid).state.phase is SessionPhase.CANCELLED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")

    run(scenario())


def test_spawn_blocked_in_acquire_cannot_resurrect_after_cleanup():
    class BlockingAcquirePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.gate = asyncio.Event()
            self.release_calls = 0
            self.release_env_calls = 0
            self.acquire_calls = 0
            self.cancellations = 0
            self.env = object()

        async def acquire(self, role):
            self.acquire_calls += 1
            self.started.set()
            while not self.gate.is_set():
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            return self.env

        async def release(self):
            self.release_calls += 1

        async def release_env(self, env):
            assert env is self.env
            self.release_env_calls += 1

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [terminal("must never start")])
    scheduler, _ = build_scheduler(lead, [child])
    pool = BlockingAcquirePool()
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="startup-race",
                kind=RowKind.CHILD_AGENT,
                order=0,
            )
        )
        spawn_task = asyncio.create_task(
            scheduler.spawn(
                0,
                "coder",
                "blocked startup",
                tool_call_id="startup-race",
            )
        )
        await pool.started.wait()
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert pool.release_calls == 0
        assert pool.cancellations >= 2
        assert scheduler._startup_tasks == {}
        assert scheduler._startup_origin == {}
        assert scheduler._startup_envs == {}
        assert scheduler._child_reservation == {}
        assert scheduler.inflight_spawn("coder", "blocked startup") is None
        startup_row = lead.state.pending_events.rows["startup-race"]
        assert startup_row.status is RowStatus.FAILED
        assert startup_row.error and "scheduler cleanup" in startup_row.error

        pool.gate.set()
        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await spawn_task

        assert set(scheduler.table.entries) == {0}
        assert set(scheduler._sessions) == {0}
        assert scheduler._tasks == {}
        assert scheduler._child_reservation == {}
        assert scheduler.inflight_spawn("coder", "blocked startup") is None
        assert pool.release_env_calls == 1

        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await scheduler.spawn(0, "coder", "after cleanup")
        assert pool.acquire_calls == 1
        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await scheduler.run("after cleanup")

    run(scenario())


def test_message_add_blocked_during_cleanup_cannot_create_late_driver():
    class BlockingAddSession(ScriptedSession):
        def __init__(self):
            super().__init__("coder", [terminal("first turn")])
            self.add_started = asyncio.Event()
            self.add_release = asyncio.Event()
            self.add_finished = asyncio.Event()
            self.cancellations = 0

        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            self.add_started.set()
            while not self.add_release.is_set():
                try:
                    await self.add_release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.state.append_message(
                {"role": "user", "content": "late add mutation"}
            )
            self.add_finished.set()

    lead = ScriptedSession("lead", [])
    child = BlockingAddSession()
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "first")
        await scheduler._tasks[aid]
        assert child.state.phase is SessionPhase.DONE
        messages_before = list(child.state.messages)

        send_task = asyncio.create_task(
            scheduler.send_message(0, aid, "late", "must stay queued")
        )
        await child.add_started.wait()
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await scheduler.cleanup(cleanup_timeout=0.01)
        with pytest.raises(asyncio.CancelledError):
            await send_task

        assert child.cancellations >= 2
        assert scheduler._message_delivery_tasks == {}
        assert scheduler._message_delivery_records == {}
        assert scheduler._tasks == {}
        assert child.state.phase is SessionPhase.DONE
        assert child.state.messages == messages_before
        assert len(scheduler._message_inbox[aid]) == 1
        assert len(child.state.pending_user_messages) == 1
        assert aid not in scheduler._child_reservation

        child.add_release.set()
        await asyncio.wait_for(child.add_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert scheduler._tasks == {}
        assert child.state.phase is SessionPhase.DONE
        assert child.state.messages == messages_before
        assert len(scheduler._message_inbox[aid]) == 1
        assert len(child.state.pending_user_messages) == 1
        assert aid not in scheduler._child_reservation

    run(scenario())


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -0.1, float("inf"), float("-inf"), float("nan"), True, "invalid", None],
)
def test_cleanup_rejects_invalid_timeout_before_any_side_effect(invalid_timeout):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        sess.state.set_phase(SessionPhase.DONE)
        return "done"

    class RecordingPool:
        def __init__(self):
            self.release_calls = 0

        async def acquire(self, role):
            return None

        async def release(self):
            self.release_calls += 1

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])
    pool = RecordingPool()
    scheduler._worktree_pool = pool

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "keep running")
        await started.wait()
        driver = scheduler._tasks[aid]
        with pytest.raises(
            ValueError,
            match="cleanup_timeout must be a finite number greater than zero",
        ):
            await scheduler.cleanup(cleanup_timeout=invalid_timeout)

        assert scheduler._shutting_down is False
        assert scheduler._tasks[aid] is driver
        assert driver.done() is False
        assert aid in scheduler._child_reservation
        assert pool.release_calls == 0

        release.set()
        await asyncio.wait_for(driver, timeout=0.5)

    run(scenario())


def test_wait_until_terminal_follows_message_replacement_created_by_finishing_task():
    class BlockingDiff:
        def __init__(self):
            self.release = asyncio.Event()

        async def get_diff(self):
            await self.release.wait()
            return ""

    class TwoTurnSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.calls = 0
            self.first_terminal = asyncio.Event()
            self.second_started = asyncio.Event()
            self.second_release = asyncio.Event()

        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()

        async def run_loop(self):
            self.calls += 1
            if self.calls == 1:
                self.state.set_phase(SessionPhase.DONE)
                self.first_terminal.set()
                return "first"
            self.second_started.set()
            await self.second_release.wait()
            self.state.set_phase(SessionPhase.DONE)
            return "second"

    diff = BlockingDiff()
    child = TwoTurnSession(diff)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await child.first_terminal.wait()
        await scheduler.send_message(0, aid, "followup", "continue")
        waiter = asyncio.create_task(scheduler.wait_until_terminal(aid))
        await asyncio.sleep(0)
        diff.release.set()
        await child.second_started.wait()
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not scheduler._tasks[aid].done()
        child.second_release.set()
        await asyncio.wait_for(waiter, timeout=0.5)
        return aid

    aid = run(scenario())

    assert child.calls == 2
    assert scheduler.table.get(aid).result == "second"
