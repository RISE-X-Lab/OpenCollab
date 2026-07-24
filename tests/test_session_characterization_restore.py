"""Session persistence and restoration characterization tests."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    fake_worktree_pool,
    llm_response,
    run,
)

from opencollab.adapters.storage import SessionStore
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase


def test_save_and_load_round_trip_restores_stopped_phase_and_reason(tmp_path):
    # The single graceful-stop terminal round-trips carrying its reason string;
    # the *why* lives in terminal_reason, not in a per-terminal phase member.
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.state.set_phase(SessionPhase.STOPPED)
    session.state.terminal_reason = "budget exceeded: 10 tokens used"
    path = tmp_path / "terminal.json"

    session.save(str(path))
    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.state.phase is SessionPhase.STOPPED
    assert loaded.state.terminal_reason == "budget exceeded: 10 tokens used"

@pytest.mark.parametrize(
    "legacy_phase",
    ["cancelled", "budget_exceeded", "step_limit_exceeded", "context_overflow"],
)
def test_restore_migrates_legacy_terminal_phase_string_to_stopped(tmp_path, legacy_phase):
    # Snapshots written before the Lane S1 terminal collapse carry the old
    # per-terminal phase strings. restore() migrates each to STOPPED (keeping its
    # terminal_reason) instead of silently degrading to IDLE via the unknown-value
    # fallback — the disposition survives the format change.
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.state.set_phase(SessionPhase.STOPPED)
    session.state.terminal_reason = "legacy detail"
    path = tmp_path / "legacy-terminal.json"
    session.save(str(path))

    snapshot = json.loads(path.read_text())
    snapshot["session_state"]["phase"] = legacy_phase
    path.write_text(json.dumps(snapshot))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())
    assert loaded.state.phase is SessionPhase.STOPPED
    assert loaded.state.terminal_reason == "legacy detail"

@pytest.mark.parametrize("phase_string", ["a_phase_that_no_longer_exists", "scheduled"])
def test_restore_of_unknown_or_retired_phase_string_falls_back_to_idle(tmp_path, phase_string):
    # A genuinely unknown phase string, and the retired pure-transitional
    # 'scheduled', both restore to IDLE (re-runnable) rather than a terminal.
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.state.set_phase(SessionPhase.STOPPED)
    session.state.terminal_reason = "budget exceeded: 10 tokens used"
    path = tmp_path / "legacy.json"
    session.save(str(path))

    snapshot = json.loads(path.read_text())
    snapshot["session_state"]["phase"] = phase_string
    path.write_text(json.dumps(snapshot))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())
    assert loaded.state.phase is SessionPhase.IDLE

def test_restore_pairs_reused_tool_call_ids_in_transcript_order(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())

    def reused_call():
        return {
            "id": "reused-id",
            "type": "function",
            "function": {"name": "grep", "arguments": "{}"},
        }

    session.state.append_message(
        {"role": "assistant", "content": None, "tool_calls": [reused_call()]}
    )
    session.state.append_message(
        {"role": "tool", "tool_call_id": "reused-id", "content": "old result"}
    )
    session.state.append_message(
        {"role": "assistant", "content": None, "tool_calls": [reused_call()]}
    )
    session.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "reused-tool-id.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    matching_results = [
        message
        for message in loaded.messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "reused-id"
    ]
    assert len(matching_results) == 2
    assert matching_results[-1]["content"] == "Tool execution interrupted by session restore."
    assert loaded.messages[-1] == matching_results[-1]
    assert loaded._open_tool_call_ids() == []

def test_restore_converts_orphaned_deferred_child_to_failed_tool_result(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.phase = SessionPhase.AWAITING_EVENTS
    session.state.pending_events.add(
        PendingRow(
            tool_call_id="child-1",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=7,
            status=RowStatus.PENDING,
        )
    )
    path = tmp_path / "pending.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    row = loaded.state.pending_events.rows["child-1"]
    assert loaded.phase is SessionPhase.AWAITING_EVENTS
    assert row.status is RowStatus.FAILED
    assert "interrupted by session restore" in (row.result or "")
    assert loaded.state.pending_events.is_complete()

def test_scheduler_init_preserves_and_drains_restored_awaiting_phase(tmp_path):
    from opencollab.application.scheduler import LaunchSpec, Scheduler

    agent = FakeAgent()
    original = Session(agent=agent, llm=FakeLLMClient())
    original.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "child-restore",
                    "type": "function",
                    "function": {"name": "spawn_agent", "arguments": "{}"},
                }
            ],
        }
    )
    original.phase = SessionPhase.AWAITING_EVENTS
    original.state.pending_events.add(
        PendingRow(
            tool_call_id="child-restore",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=9,
            status=RowStatus.PENDING,
        )
    )
    queued_xml = (
        '<teammate-message teammate_id="A1" summary="restored">\n'
        "queued after child\n"
        "</teammate-message>"
    )
    original.state.queue_pending_user_message(
        {
            "role": "user",
            "content": queued_xml,
            "message_content": "queued after child",
            "from_aid": 1,
            "to_aid": 0,
            "summary": "restored",
        }
    )
    path = tmp_path / "scheduler-resume.json"
    original.save(str(path))

    llm = FakeLLMClient(
        [
            llm_response(content="resumed after failed child"),
            llm_response(content="restored teammate handled"),
            llm_response(content="new turn handled"),
        ]
    )
    resumed = Session(agent=agent, llm=llm)

    class ResumeFactory:
        def create_lead_session(self, **kwargs):
            return resumed

    scheduler = Scheduler(
        session_factory=ResumeFactory(),
        worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(),
    )
    scheduler.create_init_process(LaunchSpec(session_file=str(path)))

    assert resumed.phase is SessionPhase.AWAITING_EVENTS
    assert resumed.state.pending_events.is_complete()
    assert len(scheduler._message_inbox[0]) == 1
    assert run(scheduler.run("new question")) == "new turn handled"
    assert resumed.state.pending_events.is_empty()
    assert resumed.state.pending_user_messages == []
    assert scheduler._message_inbox.get(0) == []
    tool_results = [m for m in resumed.messages if m.get("role") == "tool"]
    assert tool_results[-1]["tool_call_id"] == "child-restore"
    assert "interrupted by session restore" in tool_results[-1]["content"]
    assert any(m.get("role") == "tool" for m in llm.calls[0]["messages"])
    assert llm.calls[1]["messages"][-1]["content"].startswith(queued_xml)
    assert llm.calls[2]["messages"][-1]["content"].startswith("new question")

def test_restore_keeps_pending_messages_from_legacy_structured_snapshot(tmp_path):
    path = tmp_path / "legacy-structured.json"
    path.write_text(
        '{"messages":[{"role":"system","content":"sys"}],'
        '"pending_messages":[{"role":"user","content":"queued"}]}',
        encoding="utf-8",
    )

    loaded = load_session(str(path), agent=FakeAgent(), llm=FakeLLMClient())

    assert loaded.state.pending_user_messages == [
        {"role": "user", "content": "queued"}
    ]

def test_restore_closes_tool_call_from_interrupted_execution_phase(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "lost-call",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                }
            ],
        }
    )
    session.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "interrupted.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.phase is SessionPhase.IDLE
    assert loaded.messages[-1] == {
        "role": "tool",
        "tool_call_id": "lost-call",
        "content": "Tool execution interrupted by session restore.",
    }

def test_scheduler_run_clears_stale_pending_rows_from_interrupted_phase(tmp_path):
    from opencollab.application.scheduler import LaunchSpec, Scheduler

    agent = FakeAgent()
    original = Session(agent=agent, llm=FakeLLMClient())
    original.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "lost-call",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                }
            ],
        }
    )
    original.state.pending_events.add(
        PendingRow(
            tool_call_id="lost-call",
            kind=RowKind.IMMEDIATE,
            order=0,
            status=RowStatus.DONE,
            result="buffered result",
        )
    )
    original.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "interrupted-with-row.json"
    original.save(str(path))

    resumed = Session(
        agent=agent,
        llm=FakeLLMClient([llm_response(content="fresh answer")]),
    )

    class ResumeFactory:
        def create_lead_session(self, **kwargs):
            return resumed

    scheduler = Scheduler(
        session_factory=ResumeFactory(),
        worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(),
    )
    scheduler.create_init_process(LaunchSpec(session_file=str(path)))

    async def scenario():
        return await asyncio.wait_for(scheduler.run("new question"), timeout=0.25)

    assert run(scenario()) == "fresh answer"
    assert resumed.state.pending_events.is_empty()

def test_session_accepts_explicit_store():
    class FakeStore:
        def __init__(self):
            self.save_calls = []
            self.load_calls = []
            self.loaded_messages = [{"role": "system", "content": "loaded from fake store"}]

        def save(self, path, messages, *, meta=None):
            self.save_calls.append((path, copy.deepcopy(messages), meta))

        def load_messages(self, path, system_prompt):
            self.load_calls.append((path, system_prompt))
            return copy.deepcopy(self.loaded_messages)

    fake_store = FakeStore()
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient(), store=fake_store)
    session.messages.append({"role": "user", "content": "hello"})

    session.save("fake-session.jsonl")
    loaded = load_session(
        "fake-session.jsonl", agent=agent, llm=FakeLLMClient(), store=fake_store
    )

    assert session.store is fake_store
    assert len(fake_store.save_calls) == 1
    saved_path, saved_messages, _ = fake_store.save_calls[0]
    assert saved_path == "fake-session.jsonl"
    assert [m["role"] for m in saved_messages] == [m["role"] for m in session.messages]
    assert saved_messages[-1]["content"] == "hello"
    assert loaded.store is fake_store
    assert fake_store.load_calls == [("fake-session.jsonl", agent.system_prompt)]
    assert loaded.messages == fake_store.loaded_messages

def test_session_store_preserves_messages_only_jsonl_semantics(tmp_path):
    store = SessionStore()
    path = tmp_path / "stored.jsonl"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]

    store.save(str(path), messages)

    assert store.load_messages(str(path), "fallback") == messages
