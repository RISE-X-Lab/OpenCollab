"""Recovery waits retain public cancellation and failure semantics."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.spawn import SpawnAgentTool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.application.session_run import _EMPTY_STOP_PLACEHOLDER
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import FakeLLMClient, llm_response, tool_call


class HeldClient:
    def __init__(self):
        self.release = asyncio.Event()

    async def complete(self, **kwargs):
        await self.release.wait()
        return llm_response(content="child answer")

class Pool:
    def __init__(self, workspace):
        self.env = LocalEnvironment(str(workspace))

    async def acquire(self, role):
        return self.env

    async def release(self):
        pass

class Factory:
    def __init__(self, workspace):
        self.client = HeldClient()
        self.workspace = workspace

    def build_spawn_session(self, **kwargs):
        return build_session(
            agent=Agent(name=kwargs["role"], system_prompt="Finish the assigned task."),
            llm=self.client, env=kwargs["env"], aid=kwargs["aid"], max_budget_tokens=kwargs["budget"],
            seed_user_messages=[{"role": "user", "content": kwargs["task"]}] if kwargs["task"] else None,
            auto_save_path=str(self.workspace / f"child-{kwargs['aid']}.json"),
        )

def make_scheduler(workspace, *, name="source", events=None):
    factory = Factory(workspace / name)
    sink = EventBus(events.append) if events is not None else EventBus()
    return Scheduler(session_factory=factory, worktree_pool=Pool(workspace), event_sink=sink), factory

async def wait_phase(session, phase):
    async def until():
        while session.phase is not phase:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(until(), 2)
    await asyncio.gather(*session.event_bus.pending_tasks)

async def wait_redelegation(session):
    async def until():
        while session.phase is not SessionPhase.AWAITING_EVENTS or "new-child" not in session.state.pending_events.rows:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(until(), 2)
    await asyncio.gather(*session.pending_cleanup_tasks)

async def saved_recovery(workspace, kind, *, interim_answer=None):
    team, factory = make_scheduler(workspace)
    path = workspace / "source.json"
    calls = [tool_call("old-child", "spawn_agent", '{"role":"worker","task":"old child"}')]
    model = (
        FakeLLMClient([llm_response(content=interim_answer, tool_calls=calls), llm_response(content="old answer")])
        if kind == "awaiting" else HeldClient()
    )
    source = build_session(
        agent=Agent(name="lead", system_prompt="Finish the task.", tools=[SpawnAgentTool(team)]),
        llm=model, env=LocalEnvironment(str(workspace)), auto_save_path=str(path),
    )
    team.register_lead(source)
    active = None
    if kind == "queued":
        await source.add_user_message("old accepted request")
        await asyncio.gather(*source.pending_cleanup_tasks)
    else:
        if kind == "inbox":
            sender = await team.spawn(0, "sender", "prepare update")
        active = asyncio.create_task(team.run("old request"))
        await wait_phase(source, SessionPhase.AWAITING_EVENTS if kind == "awaiting" else SessionPhase.CALLING_LLM)
        if kind == "awaiting":
            await source.enqueue_auto_save()
        if kind == "inbox":
            assert await team.send_message(sender, 0, "update", "recover this message") == "Message queued to aid 0."
            await asyncio.gather(*source.event_bus.pending_tasks)
    return path, team, factory, model, active

async def close_original(team, factory, model, active):
    factory.client.release.set()
    if isinstance(model, HeldClient):
        model.release.set()
    await team.cleanup()
    if active is not None:
        await asyncio.gather(active, return_exceptions=True)

@pytest.mark.parametrize("kind", ["awaiting", "queued", "inbox"])
@pytest.mark.parametrize("cancellation", ["event", "caller"])
async def test_restored_turn_cancellation_settles_owned_child_without_accepting_new_request(
    tmp_path, kind, cancellation,
):
    path, original, old_factory, old_model, old_active = await saved_recovery(tmp_path, kind)
    team, factory = make_scheduler(tmp_path, name="resumed")
    model = FakeLLMClient([
        llm_response(tool_calls=[tool_call("new-child", "spawn_agent", '{"role":"worker","task":"finish recovery"}')]),
        llm_response(content="unused answer"),
    ])
    restored = load_session(
        str(path), agent=Agent(name="lead", system_prompt="Finish the task.", tools=[SpawnAgentTool(team)]),
        llm=model, env=LocalEnvironment(str(tmp_path)), auto_save_path=str(tmp_path / "restored.json"),
    )
    team.register_lead(restored)
    unrelated = await team.spawn(0, "sender", "unrelated work") if kind == "inbox" else None
    cancel_event = asyncio.Event()
    active = asyncio.create_task(team.run("new request", cancel_event=cancel_event))
    try:
        await wait_redelegation(restored)
        if cancellation == "event":
            cancel_event.set()
            with pytest.raises(SchedulerTurnError, match="interrupted by user"):
                await asyncio.wait_for(active, 2)
            assert restored.phase is SessionPhase.STOPPED
            assert restored.state.pending_events.is_empty()
            assert team._shutting_down is False
            if unrelated is not None:
                assert team.team_snapshot()[unrelated]["busy"] is True
        else:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(active, 2)
            assert team._shutting_down is True
        await asyncio.gather(*restored.pending_cleanup_tasks)
        assert len(model.calls) == 1
        assert all(message.get("content") != "new request" for message in restored.messages)
        snapshot = SessionStore().load_snapshot(restored.auto_save_path, restored.agent.system_prompt)
        assert snapshot["session_state"]["phase"] == "stopped"
        if cancellation == "event":
            assert snapshot["session_state"]["pending_events"] == []
        else:
            assert all(row["status"] == "failed" for row in snapshot["session_state"]["pending_events"])
        assert all(message.get("content") != "new request" for message in snapshot["messages"])
    finally:
        factory.client.release.set()
        await team.cleanup()
        await asyncio.gather(active, return_exceptions=True)
        await close_original(original, old_factory, old_model, old_active)

@pytest.mark.parametrize("kind", ["awaiting", "queued", "inbox"])
async def test_failed_restored_turn_keeps_error_visible_and_accepts_new_request(tmp_path, kind):
    path, original, old_factory, old_model, old_active = await saved_recovery(tmp_path, kind)
    events = []
    team, factory = make_scheduler(tmp_path, name="resumed", events=events)
    model = FakeLLMClient([RuntimeError("recovery provider failed"), llm_response(content="new answer")])
    restored = load_session(
        str(path), agent=Agent(name="lead", system_prompt="Finish the task."), llm=model,
        env=LocalEnvironment(str(tmp_path)), auto_save_path=str(tmp_path / "restored-error.json"),
    )
    team.register_lead(restored)
    if kind == "inbox":
        await team.spawn(0, "sender", "unrelated work")
        factory.client.release.set()
    try:
        assert await asyncio.wait_for(team.run("new request"), 2) == "new answer"
        assert len(model.calls) == 2
        assert all(message.get("content") != "new request" for message in model.calls[0]["messages"])
        assert any("recovery provider failed" in str(event.data) for event in events if event.type == "agent_failed")
        await asyncio.gather(*restored.pending_cleanup_tasks)
        snapshot = SessionStore().load_snapshot(restored.auto_save_path, restored.agent.system_prompt)
        assert snapshot["session_state"]["phase"] == "done"
    finally:
        factory.client.release.set()
        await team.cleanup()
        await close_original(original, old_factory, old_model, old_active)

@pytest.mark.parametrize(
    ("replies", "expected_answer"),
    [
        (["old answer"], "old answer"), ([None, None], None), ([None, "old answer"], "old answer"),
        ([_EMPTY_STOP_PLACEHOLDER], _EMPTY_STOP_PLACEHOLDER),
        ([None, _EMPTY_STOP_PLACEHOLDER], _EMPTY_STOP_PLACEHOLDER),
    ],
    ids=["direct-answer", "empty-twice", "retry-answer", "same-text", "retry-same-text"],
)
async def test_cancellation_at_recovery_completion_leaves_new_request_unaccepted(
    tmp_path, replies, expected_answer,
):
    path, original, old_factory, old_model, old_active = await saved_recovery(tmp_path, "queued")
    cancel_event = asyncio.Event()

    def cancel_at_completion(event):
        if event.type == "agent_completed" and event.data["aid"] == 0:
            cancel_event.set()

    factory = Factory(tmp_path / "resumed")
    team = Scheduler(session_factory=factory, worktree_pool=Pool(tmp_path), event_sink=EventBus(cancel_at_completion))
    model = FakeLLMClient([llm_response(content=reply) for reply in replies])
    restored = load_session(
        str(path), agent=Agent(name="lead", system_prompt="Finish the task."), llm=model,
        env=LocalEnvironment(str(tmp_path)), auto_save_path=str(tmp_path / "restored-completion.json"),
    )
    team.register_lead(restored)
    try:
        with pytest.raises(SchedulerTurnError, match="interrupted by user") as caught:
            await team.run("new request", cancel_event=cancel_event)
        assert caught.value.partial_answer == expected_answer
        assert len(model.calls) == len(replies)
        assert all(message.get("content") != "new request" for message in restored.messages)
        await asyncio.gather(*restored.pending_cleanup_tasks)
        snapshot = SessionStore().load_snapshot(restored.auto_save_path, restored.agent.system_prompt)
        assert snapshot["session_state"]["phase"] == "stopped"
    finally:
        await team.cleanup()
        await close_original(original, old_factory, old_model, old_active)

@pytest.mark.parametrize(
    ("finish_reason", "content", "reason", "expected_answer"),
    [
        pytest.param("content_filter", None, "provider filtered the output", None, id="filtered-empty"),
        pytest.param(
            "content_filter", "visible reply", "provider filtered the output", "visible reply", id="filtered-visible",
        ),
        pytest.param("stop", "final reply", "interrupted by user", "final reply", id="completed"),
        pytest.param(
            "length", None, "output truncated: provider reached its generation limit",
            "interim planning text", id="truncated",
        ),
    ],
)
async def test_cancellation_at_waiting_recovery_completion_reports_the_turn_answer(
    tmp_path, finish_reason, content, reason, expected_answer,
):
    path, original, old_factory, old_model, old_active = await saved_recovery(
        tmp_path, "awaiting", interim_answer="interim planning text",
    )
    cancel_event = asyncio.Event()

    def cancel_at_completion(event):
        if event.type in {"agent_completed", "agent_failed"} and event.data["aid"] == 0:
            cancel_event.set()

    team = Scheduler(
        session_factory=old_factory, worktree_pool=Pool(tmp_path), event_sink=EventBus(cancel_at_completion),
    )
    model = FakeLLMClient([llm_response(content=content, finish_reason=finish_reason)])
    restored = load_session(
        str(path), agent=Agent(name="lead", system_prompt="Finish the task."),
        llm=model, env=LocalEnvironment(str(tmp_path)), auto_save_path=str(tmp_path / "restored-waiting.json"),
    )
    team.register_lead(restored)
    try:
        assert restored.phase is SessionPhase.AWAITING_EVENTS
        with pytest.raises(SchedulerTurnError) as caught:
            await asyncio.wait_for(team.run("new request", cancel_event=cancel_event), 3)
        assert caught.value.terminal_reason == reason
        assert caught.value.partial_answer == expected_answer
        assert len(model.calls) == 1
        assert any(message["role"] == "tool" for message in model.calls[0]["messages"])
        assert all(message.get("content") != "new request" for message in restored.messages)
    finally:
        await team.cleanup()
        await close_original(original, old_factory, old_model, old_active)
