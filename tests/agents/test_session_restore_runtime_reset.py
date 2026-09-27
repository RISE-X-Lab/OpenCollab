"""Regression coverage for restoring a reused session runtime."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from opencollab.application.session import SessionBusyError
from opencollab.bootstrap import build_session as Session
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    llm_response,
    run,
)


def test_restore_reused_session_resets_live_turn_cursor(tmp_path):
    baseline_tool = SimpleNamespace(
        to_openai_schema=lambda: {
            "type": "function",
            "function": {"name": "baseline", "parameters": {}},
        }
    )
    agent = FakeAgent(tools=[baseline_tool])
    snapshot_source = Session(agent=agent, llm=FakeLLMClient(), max_steps=1)
    snapshot_source.state.replace_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "OLD SNAPSHOT ANSWER"},
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "child-current",
                "type": "function",
                "function": {"name": "spawn_agent", "arguments": "{}"},
            }],
        },
    ])
    snapshot_source.state.set_step_count(1)
    snapshot_source.state.set_phase(SessionPhase.AWAITING_EVENTS)
    snapshot_source.state.active_turn_start_message_index = 4
    snapshot_source.state.pending_events.add(PendingRow(
        tool_call_id="child-current",
        kind=RowKind.CHILD_AGENT,
        order=0,
        ref=1,
        status=RowStatus.PENDING,
    ))
    path = tmp_path / "reused-awaiting-cursor.json"
    snapshot_source.save(str(path))

    llm = FakeLLMClient([llm_response(content="prior live answer")])
    reused = Session(agent=agent, llm=llm, max_steps=1)
    run(reused.add_user_message("prior live question"))
    assert run(reused.run_loop()) == "prior live answer"
    assert reused.runner._turn_start_message_index is not None
    reused.agent.tools = []
    reused.agent.tool_choice = {
        "type": "function",
        "function": {"name": "stale"},
    }

    reused.restore(str(path))

    assert reused.state.active_turn_start_message_index == 4
    assert reused.runner._turn_start_message_index is None
    assert reused.agent.tools == [baseline_tool]
    assert reused.agent.tool_choice is None
    assert run(reused.run_loop()) == ""
    assert reused.state.phase is SessionPhase.STOPPED
    assert len(llm.calls) == 1


def test_restore_reused_session_rearms_empty_response_retry(tmp_path):
    agent = FakeAgent()
    snapshot_source = Session(agent=agent, llm=FakeLLMClient())
    snapshot_source.state.replace_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "child-current",
                "type": "function",
                "function": {"name": "spawn_agent", "arguments": "{}"},
            }],
        },
    ])
    snapshot_source.state.set_phase(SessionPhase.AWAITING_EVENTS)
    snapshot_source.state.active_turn_start_message_index = 1
    snapshot_source.state.pending_events.add(PendingRow(
        tool_call_id="child-current",
        kind=RowKind.CHILD_AGENT,
        order=0,
        ref=1,
        status=RowStatus.DONE,
        result="child result",
    ))
    path = tmp_path / "reused-empty-retry.json"
    snapshot_source.save(str(path))

    llm = FakeLLMClient([
        llm_response(content=None),
        llm_response(content="prior recovered"),
        llm_response(content=None),
        llm_response(content="restored recovered"),
    ])
    reused = Session(agent=agent, llm=llm)
    run(reused.add_user_message("prior question"))
    assert run(reused.run_loop()) == "prior recovered"
    assert reused.runner._empty_stop_retried is True

    reused.restore(str(path))

    assert reused.runner._empty_stop_retried is False
    assert run(reused.run_loop()) == "restored recovered"
    assert len(llm.calls) == 4


@pytest.mark.parametrize("cursor", [None, 10_000])
def test_restore_awaiting_legacy_cursor_returns_resumed_answer(tmp_path, cursor):
    agent = FakeAgent()
    source = Session(agent=agent, llm=FakeLLMClient())
    source.state.replace_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "child-current",
                "type": "function",
                "function": {"name": "spawn_agent", "arguments": "{}"},
            }],
        },
    ])
    source.state.set_phase(SessionPhase.AWAITING_EVENTS)
    source.state.active_turn_start_message_index = 4
    source.state.pending_events.add(PendingRow(
        tool_call_id="child-current",
        kind=RowKind.CHILD_AGENT,
        order=0,
        ref=1,
        status=RowStatus.DONE,
        result="child result",
    ))
    path = tmp_path / "awaiting-legacy-cursor.json"
    source.save(str(path))
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if cursor is None:
        snapshot["session_state"].pop("active_turn_start_message_index")
    else:
        snapshot["session_state"]["active_turn_start_message_index"] = cursor
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    restored = Session(
        agent=agent,
        llm=FakeLLMClient([llm_response(content="answer after legacy restore")]),
    )
    restored.restore(str(path))

    assert restored.state.active_turn_start_message_index == 5
    assert run(restored.run_loop()) == "answer after legacy restore"


def test_restore_rejects_a_session_with_an_active_turn(tmp_path):
    agent = FakeAgent()
    source = Session(agent=agent, llm=FakeLLMClient())
    path = tmp_path / "busy-restore.json"
    source.save(str(path))

    session = Session(agent=agent, llm=FakeLLMClient())
    run(session._turn_lock.acquire())
    try:
        with pytest.raises(SessionBusyError, match="runtime work is active"):
            session.restore(str(path))
    finally:
        session._turn_lock.release()


@pytest.mark.asyncio
async def test_restore_rejects_pending_runtime_cleanup(tmp_path):
    agent = FakeAgent()
    source = Session(agent=agent, llm=FakeLLMClient())
    path = tmp_path / "cleanup-restore.json"
    source.save(str(path))

    session = Session(agent=agent, llm=FakeLLMClient())
    blocker = asyncio.Event()
    cleanup_task = asyncio.create_task(blocker.wait())
    session.tool_execution._pending_cleanup_tasks.add(cleanup_task)
    try:
        with pytest.raises(SessionBusyError, match="runtime work is active"):
            session.restore(str(path))
    finally:
        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task
