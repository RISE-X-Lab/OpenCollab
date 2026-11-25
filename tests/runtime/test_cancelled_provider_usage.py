"""Real session and scheduler accounting for providers that outlive cancellation."""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.spawn import SpawnAgentTool
from opencollab.adapters.trace import Tracer
from opencollab.application.event_bus import EventBus
from opencollab.application.run_control import RunControl, RunEvent
from opencollab.application.scheduler import Scheduler, SchedulerTurnError
from opencollab.application.session import SessionBusyError
from opencollab.application.session_run import GenerationTimeoutError
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.session import SessionPhase
from tests.runtime.test_async_compaction import history
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    fake_worktree_pool,
    llm_response,
    tool_call,
)


class _DelayedProvider:
    def __init__(self, outcome="success"):
        self.outcome = outcome
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_count = 0

    def context_window(self):
        return 40_000

    async def complete(self, messages=None, **kwargs):
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_count += 1
                self.cancelled.set()
                if self.outcome == "cancelled":
                    raise
        if self.outcome == "error":
            raise RuntimeError("late provider failure")
        return llm_response(content="provider response", input_tokens=13, output_tokens=24)

async def _drain(session):
    while session.pending_cleanup_tasks or session.runner.pending_cleanup_tasks:
        await asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True)
        await asyncio.sleep(0)

def _message_payloads(messages):
    return [{key: value for key, value in message.items() if key != "timestamp"} for message in messages]

async def _abandon_call(session, provider, trigger):
    call = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(provider.started.wait(), 1)
    if trigger == "cancel":
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, 1)
    else:
        with pytest.raises(GenerationTimeoutError):
            await asyncio.wait_for(call, 1)
    await asyncio.wait_for(provider.cancelled.wait(), 1)

@pytest.mark.parametrize("protected_call", [False, True])
@pytest.mark.parametrize(
    ("trigger", "outcome", "repeat_cancel"),
    [
        ("normal", "success", False),
        ("cancel", "success", False),
        ("cancel", "success", True),
        ("timeout", "success", False),
        ("timeout", "success", True),
        ("cancel", "error", False),
        ("timeout", "error", True),
        ("cancel", "cancelled", False),
        ("timeout", "cancelled", False),
    ],
)
async def test_session_accounts_only_successful_abandoned_response_once(
    local_environment_factory,
    trigger, outcome, repeat_cancel, protected_call, tmp_path,
):
    provider = _DelayedProvider(outcome)
    path = tmp_path / "automatic.json"
    session = build_session(
        env=local_environment_factory(),
        agent=FakeAgent(), llm=provider, llm_timeout=0.01 if trigger == "timeout" else 600,
        auto_save_path=str(path),
    )
    await session.add_user_message("run")
    if protected_call:
        session.state.wind_down_done = True
        session.runner._commit_reserve = session.max_budget_tokens - 30
    call = asyncio.create_task(session.run_loop())
    try:
        await asyncio.wait_for(provider.started.wait(), 1)
        if trigger == "cancel":
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, 1)
            expected_phase = SessionPhase.STOPPED
        elif trigger == "timeout":
            with pytest.raises(GenerationTimeoutError):
                await asyncio.wait_for(call, 1)
            expected_phase = SessionPhase.ERROR
        else:
            provider.release.set()
            assert await asyncio.wait_for(call, 1) == "provider response"
            expected_phase = SessionPhase.DONE
        if trigger != "normal":
            await asyncio.wait_for(provider.cancelled.wait(), 1)
        assert session.phase is expected_phase
        assert session.used_tokens == (37 if trigger == "normal" else 0)

        if trigger != "normal" and outcome != "cancelled":
            pending = session.pending_cleanup_tasks
            assert len(pending) == 1
            with pytest.raises(SessionBusyError):
                await session.add_user_message("next turn")
            with pytest.raises(SessionBusyError):
                session.restore(str(tmp_path / "unread.json"))
            if repeat_cancel:
                pending[0].cancel()
                await asyncio.sleep(0)
                assert provider.cancel_count == 2
                assert session.pending_cleanup_tasks == pending

        provider.release.set()
        await asyncio.wait_for(_drain(session), 1)
        expected_usage = 37 if outcome == "success" else 0
        expected_late_usage = (37,) if outcome == "success" and trigger != "normal" else ()
        expected_reserve_consumed = protected_call and outcome == "success"
        assert session.used_tokens == expected_usage
        assert session.state.budget_reserve_consumed is expected_reserve_consumed
        assert session.runner.late_provider_usage == expected_late_usage
        assert session.phase is expected_phase
        if trigger != "normal":
            assert all(message.get("content") != "provider response" for message in session.messages)
        assert session.pending_cleanup_tasks == ()
        assert session.runner.pending_cleanup_tasks == ()

        snapshot = SessionStore().load_snapshot(str(path), session.agent.system_prompt)
        assert snapshot["session_state"]["used_tokens"] == expected_usage
        assert snapshot["session_state"]["budget_reserve_consumed"] is expected_reserve_consumed
        assert _message_payloads(snapshot["messages"]) == session.messages
        restored = load_session(str(path), env=local_environment_factory(), agent=FakeAgent(), llm=provider)
        assert restored.used_tokens == expected_usage
        assert restored.state.budget_reserve_consumed is expected_reserve_consumed
        assert restored.phase is expected_phase
        assert restored.messages == session.messages
        assert session.persistence_errors == ()
        provider.outcome = "success"
        await session.add_user_message("next turn")
        assert await session.run_loop() == "provider response"
        assert session.used_tokens == expected_usage + 37
        assert session.runner.late_provider_usage == expected_late_usage
        assert SessionStore().load_snapshot(str(path), session.agent.system_prompt)["session_state"]["used_tokens"] == (
            expected_usage + 37
        )
    finally:
        provider.release.set()
        await asyncio.gather(call, return_exceptions=True)
        await _drain(session)

@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
async def test_late_usage_autosave_failure_remains_visible(local_environment_factory, trigger, tmp_path):
    error = OSError("late usage save failed")

    class FailingStore(SessionStore):
        fail_append = False

        def append_snapshot_delta(self, *args, **kwargs):
            if self.fail_append:
                raise error
            return super().append_snapshot_delta(*args, **kwargs)

    provider = _DelayedProvider()
    store = FailingStore()
    path = tmp_path / "failing.json"
    session = build_session(
        env=local_environment_factory(),
        agent=FakeAgent(), llm=provider, llm_timeout=0.01 if trigger == "timeout" else 600,
        auto_save_path=str(path), store=store,
    )
    await session.add_user_message("run")
    try:
        await _abandon_call(session, provider, trigger)
        assert session.persistence_errors == ()
        store.fail_append = True
        provider.release.set()
        await asyncio.wait_for(_drain(session), 1)
        assert session.used_tokens == 37
        assert session.runner.late_provider_usage == (37,)
        assert session.persistence_errors == (error,)
        assert store.load_snapshot(str(path), session.agent.system_prompt)["session_state"]["used_tokens"] == 0

        store.fail_append = False
        await session.add_user_message("next turn")
        assert await session.run_loop() == "provider response"
        assert session.used_tokens == 74
        assert store.load_snapshot(str(path), session.agent.system_prompt)["session_state"]["used_tokens"] == 74
        assert session.persistence_errors == (error,)
    finally:
        provider.release.set()
        await _drain(session)

@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
async def test_late_summary_usage_is_automatically_saved(local_environment_factory, trigger, tmp_path):
    provider = _DelayedProvider()
    path = tmp_path / "summary.json"
    tracer = Tracer(run_id="late-summary", output_dir=str(tmp_path))
    session = build_session(
        env=local_environment_factory(),
        agent=FakeAgent(), llm=provider, llm_timeout=0.01 if trigger == "timeout" else None,
        auto_save_path=str(path), tracer=tracer,
    )
    session.messages = history()
    session.state.set_context_tokens(23)
    try:
        await _abandon_call(session, provider, trigger)
        assert session.used_tokens == 0
        original_messages = session.messages.copy()
        provider.release.set()
        await asyncio.wait_for(_drain(session), 1)
        assert session.used_tokens == 37
        assert session.runner.late_provider_usage == (37,)
        assert session.state.context_tokens == 23
        assert session.step_count == 0
        assert session.messages == original_messages
        snapshot = SessionStore().load_snapshot(str(path), session.agent.system_prompt)
        assert snapshot["session_state"]["used_tokens"] == 37
        assert snapshot["session_state"]["context_tokens"] == 23
        assert _message_payloads(snapshot["messages"]) == original_messages
        restored = load_session(str(path), env=local_environment_factory(), agent=FakeAgent(), llm=provider)
        assert restored.used_tokens == 37
        assert restored.state.context_tokens == 23
        assert restored.messages == original_messages
        assert restored.step_count == 0
        assert session.persistence_errors == ()
        tracer.flush()
        with open(tracer.path, encoding="utf-8") as handle:
            calls = [row for line in handle if (row := json.loads(line))["type"] == "llm_call"]
        call, = calls
        assert call["metrics"]["tokens"] == 37
        assert call["payload"]["purpose"] == "summary"
        assert call["payload"]["late"] is True
    finally:
        provider.release.set()
        await _drain(session)
        tracer.close()

async def test_summary_usage_cancellation_keeps_session_reusable(local_environment_factory):
    provider = _DelayedProvider()
    provider.release.set()
    summary_usage = []

    def cancel_on_first_summary(event: RunEvent) -> None:
        if event.type == "usage" and event.data.get("purpose") == "summary":
            summary_usage.append(event.used_tokens)
            if len(summary_usage) == 1:
                turn.cancel()

    session = build_session(
        env=local_environment_factory(), agent=FakeAgent(), llm=provider, llm_timeout=600,
        run_control=RunControl(history_trigger_tokens=512, on_event=cancel_on_first_summary),
    )
    session.messages = history()
    turn = asyncio.create_task(session.run_loop())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 5)

    assert summary_usage == [37]
    assert session.used_tokens == 37
    assert session.phase is SessionPhase.STOPPED
    assert session.runner.pending_cleanup_tasks == ()
    await session.add_user_message("next turn")
    assert await asyncio.wait_for(session.run_loop(), 5) == "provider response"

async def test_session_accounts_provider_success_when_cancellation_races_with_completion(local_environment_factory):
    class Provider:
        async def complete(self, **kwargs):
            call.cancel()
            return llm_response(content="racing response", input_tokens=13, output_tokens=24)

    session = build_session(env=local_environment_factory(), agent=FakeAgent(), llm=Provider())
    await session.add_user_message("run")
    call = asyncio.create_task(session.run_loop())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 1)
    await asyncio.wait_for(_drain(session), 1)

    assert session.phase is SessionPhase.STOPPED
    assert session.used_tokens == 37
    assert session.runner.late_provider_usage == (37,)
    assert all(message.get("content") != "racing response" for message in session.messages)

class _ChildFactory:
    def __init__(self, provider, auto_save_path=None):
        self.provider = provider
        self.auto_save_path = auto_save_path
        self.session = None

    def build_spawn_session(self, **kwargs):
        agent = FakeAgent()
        agent.name = kwargs["role"]
        self.session = build_session(
            agent=agent, llm=self.provider, env=kwargs["env"], aid=kwargs["aid"], max_budget_tokens=kwargs["budget"],
            auto_save_path=self.auto_save_path,
        )
        return self.session

@pytest.mark.parametrize("cancel", [False, True])
async def test_scheduler_preserves_child_usage_after_parent_cancellation(
    local_environment_factory, cancel, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider = _DelayedProvider()
    path = tmp_path / "child.json"
    factory = _ChildFactory(provider, str(path))
    scheduler = Scheduler(
        session_factory=factory, worktree_pool=fake_worktree_pool(), event_sink=EventBus(),
    )
    lead_model = FakeLLMClient([
        llm_response(tool_calls=[tool_call(
            "delegation", "spawn_agent", '{"role":"coder","task":"controlled work"}',
        )], finish_reason="tool_calls"),
        llm_response(content="lead response"),
    ])
    lead = build_session(
        env=local_environment_factory(),
        agent=FakeAgent(tools=[SpawnAgentTool(scheduler)]), llm=lead_model)
    scheduler.register_lead(lead)
    cancel_event = asyncio.Event()
    turn = asyncio.create_task(scheduler.run("run", cancel_event=cancel_event))
    try:
        await asyncio.wait_for(provider.started.wait(), 1)

        async def wait_for_suspension():
            while lead.phase is not SessionPhase.AWAITING_EVENTS:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_suspension(), 1)
        if cancel:
            cancel_event.set()
            with pytest.raises(SchedulerTurnError, match="interrupted by user"):
                await asyncio.wait_for(turn, 1)
            await asyncio.wait_for(provider.cancelled.wait(), 1)
            assert factory.session.pending_cleanup_tasks
            assert factory.session.used_tokens == 0
        provider.release.set()
        if cancel:
            await asyncio.wait_for(_drain(factory.session), 1)
            assert factory.session.phase is SessionPhase.STOPPED
            assert factory.session.runner.late_provider_usage == (37,)
            assert all(message.get("content") != "provider response" for message in factory.session.messages)
        else:
            assert await asyncio.wait_for(turn, 1) == "lead response"
            assert factory.session.runner.late_provider_usage == ()
        assert factory.session.used_tokens == 37
        snapshot = SessionStore().load_snapshot(str(path), factory.session.agent.system_prompt)
        assert snapshot["session_state"]["used_tokens"] == 37
        restored = load_session(str(path), env=local_environment_factory(), agent=FakeAgent(), llm=provider)
        assert restored.used_tokens == 37
        assert restored.messages == factory.session.messages
        assert scheduler.table.get(1).state.used_tokens == 37
        assert scheduler.used_tokens == lead.used_tokens + 37
    finally:
        provider.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await _drain(factory.session)
        await scheduler.cleanup()
