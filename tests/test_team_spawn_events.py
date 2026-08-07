"""Scheduler spawn and live-session autosave event tests.

The broader cleanup/review characterization remains in
``test_team_event_emission``; this file owns the spawn lifecycle slice.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from test_team_event_emission import (
    _build_scheduler,
    _FakeLeadSession,
    _FakeTeammateSession,
    _scheduler_events,
    _spawn_and_settle,
    run,
)

from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase


def test_spawn_emits_agent_spawned_then_completed(monkeypatch):
    scheduler, events = _build_scheduler(monkeypatch, {"coder": ["coder did it"]})

    run(_spawn_and_settle(scheduler, 0, "coder", "do the thing"))

    seq = _scheduler_events(events)
    types = [e.type for e in seq]
    assert "agent_spawned" in types
    assert "agent_completed" in types

    spawned = [e for e in seq if e.type == "agent_spawned"][0]
    assert spawned.data["role"] == "coder"
    assert spawned.data["task"] == "do the thing"

    completed = [e for e in seq if e.type == "agent_completed"][0]
    assert completed.data["role"] == "coder"
    assert "latency" in completed.data
    assert isinstance(completed.data["latency"], float)


def test_spawn_autosaves_parent_tool_call(monkeypatch, tmp_path):
    scheduler, _ = _build_scheduler(monkeypatch, {"analyst": ["done"]})
    lead = scheduler.lead_session
    lead.auto_save_path = str(tmp_path / "agent_0_lead.json")
    lead.state.append_message({"role": "user", "content": "fix it"})
    lead.state.append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "spawn_agent",
                        "arguments": '{"role": "analyst", "task": "investigate"}',
                    },
                }
            ],
        }
    )

    run(_spawn_and_settle(scheduler, 0, "analyst", "investigate"))

    with open(lead.auto_save_path) as f:
        saved = json.load(f)
    assert saved["messages"][-1]["tool_calls"][0]["function"]["name"] == "spawn_agent"


def test_spawn_lifecycle_slow_autosave_keeps_event_loop_responsive(tmp_path):
    started = threading.Event()
    release = threading.Event()
    scheduler, _ = _build_scheduler(None, {"analyst": ["done"]})
    lead = scheduler.lead_session
    lead.auto_save_path = str(tmp_path / "lead.json")

    def slow_save(path: str) -> None:
        started.set()
        assert release.wait(timeout=2.0)
        _FakeLeadSession.save(lead, path)

    lead.save = slow_save

    async def scenario():
        aid = await scheduler.spawn(0, "analyst", "investigate")
        assert await asyncio.to_thread(started.wait, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.02)
        owners = scheduler._fallback_autosavers[0].pending_tasks
        assert owners
        release.set()
        await asyncio.gather(*owners, return_exceptions=True)
        await scheduler.wait_until_terminal(aid)
        await scheduler.cleanup()

    run(scenario())


def test_message_queue_slow_autosave_keeps_event_loop_responsive(tmp_path):
    started = threading.Event()
    release = threading.Event()
    scheduler, _ = _build_scheduler(None, {})
    target = _FakeTeammateSession("unused", role="coder")
    target.state.aid = 1
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    target.auto_save_path = str(tmp_path / "target.json")

    def slow_save(path: str) -> None:
        started.set()
        assert release.wait(timeout=2.0)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"messages": target.state.enriched_messages()}, handle)

    target.save = slow_save
    scheduler.table.add(
        SessionControlBlock(
            aid=1,
            parent_aid=0,
            agent=target.agent,
            state=target.state,
        )
    )
    scheduler._sessions[1] = target

    async def scenario():
        result = await scheduler.send_message(0, 1, "question", "are you there?")
        assert result == "Message queued to aid 1."
        assert await asyncio.to_thread(started.wait, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.02)
        owners = scheduler._fallback_autosavers[1].pending_tasks
        assert owners
        release.set()
        await asyncio.gather(*owners, return_exceptions=True)
        target.auto_save_path = None
        await scheduler.cleanup()

    run(scenario())


def test_cleanup_autosaves_live_sessions(monkeypatch, tmp_path):
    scheduler, _ = _build_scheduler(monkeypatch, {})
    lead = scheduler.lead_session
    lead.auto_save_path = str(tmp_path / "agent_0_lead.json")
    lead.state.append_message({"role": "assistant", "content": "latest state"})

    run(scheduler.cleanup())

    with open(lead.auto_save_path) as f:
        saved = json.load(f)
    assert saved["messages"][-1]["content"] == "latest state"
