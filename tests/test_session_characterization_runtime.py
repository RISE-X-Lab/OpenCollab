"""Session runtime configuration and checkpoint tests."""

from __future__ import annotations

import asyncio

from session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    fake_team_session_factory,
    fake_worktree_pool,
)

from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session


def test_session_closes_only_composition_owned_llm(monkeypatch):
    from opencollab.bootstrap import container as session_module

    class ClosableLLM(FakeLLMClient):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    owned = ClosableLLM()
    monkeypatch.setattr(session_module, "LLMClient", lambda **_kwargs: owned)
    owned_session = Session(agent=FakeAgent())
    asyncio.run(owned_session.aclose())
    asyncio.run(owned_session.aclose())

    injected = ClosableLLM()
    injected_session = Session(agent=FakeAgent(), llm=injected)
    asyncio.run(injected_session.aclose())

    assert owned.close_calls == 1
    assert injected.close_calls == 0


def test_session_runtime_config_mutations_update_all_runtime_consumers():
    old_env = object()
    new_env = object()
    old_max_steps = 7
    new_max_steps = 3
    old_budget = 1_000
    new_budget = 200
    old_tracer = object()
    new_tracer = object()
    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        env=old_env,
        max_steps=old_max_steps,
        max_budget_tokens=old_budget,
        tracer=old_tracer,
    )

    session.env = new_env
    session.max_steps = new_max_steps
    session.max_budget_tokens = new_budget
    session.tracer = new_tracer

    assert session.env is new_env
    assert session.tool_execution.environment is new_env
    assert session.max_steps == session.runner.max_steps == new_max_steps
    assert session.max_budget_tokens == session.runner.max_budget_tokens == new_budget
    assert session.tracer is new_tracer
    assert session.tool_execution.tracer is new_tracer
    assert session.runner.tracer is new_tracer

def test_scheduler_init_process_lead_uses_workspace_local_env(tmp_path, monkeypatch):
    import os

    from opencollab.adapters.env import LocalEnvironment
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(lead_workspace=str(tmp_path))

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    lead = scheduler._lead_session
    assert isinstance(lead.env, LocalEnvironment)
    assert lead.env.workspace == os.path.abspath(str(tmp_path))
    assert lead.tool_execution.environment is lead.env
    tool_names = {t.name for t in lead.agent.tools}
    assert {"spawn_agent", "spawn_with_review"} <= tool_names

def test_scheduler_init_process_lead_gets_workspace_safety_policy(tmp_path, monkeypatch):
    from opencollab.adapters.safety import SandboxInterceptor
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(lead_workspace=str(tmp_path))

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    policy = scheduler._lead_session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())

def test_lead_safety_policy_is_independent_of_child_safety_factory(tmp_path, monkeypatch):
    from opencollab.adapters.safety import SandboxInterceptor
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    # The child-session ``safety_policy_factory`` does not govern the lead: the
    # lead always gets a workspace-rooted sandbox built from its local env.
    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(
        safety_policy_factory=None,
        lead_workspace=str(tmp_path),
    )

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    policy = scheduler._lead_session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())

def test_save_and_load_round_trip_restores_runtime_state(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    session.state.pending_user_messages = [
        {"role": "user", "content": "queued", "timestamp": "2026-01-01T00:00:00Z"}
    ]
    path = tmp_path / "session.jsonl"

    session.save(str(path))
    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.messages == session.messages
    assert loaded.used_tokens == 123
    assert loaded.step_count == 4
    assert loaded.is_done is True
    assert loaded.state.pending_user_messages == session.state.pending_user_messages

def test_save_and_load_round_trip_restores_control_flow_latches(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    state = session.state
    state.turn.reads_since_last_edit = 7
    state.turn.low_yield_since_progress = 3
    state.turn.distinct_evidence_count = 4
    state.turn.seen_result_hashes = {"content-hash", "call-hash"}
    state.turn.scout_ledger = [{"tool": "grep", "outcome": "hit"}]
    state.turn.steps_since_progress = 2
    state.wind_down_done = True
    state.wind_down_token_mark = 123
    state.turn.loop_blocked_since_progress = 2
    path = tmp_path / "control-state.json"

    session.save(str(path))
    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    restored = loaded.state
    assert restored.turn.reads_since_last_edit == 7
    assert restored.turn.low_yield_since_progress == 3
    assert restored.turn.distinct_evidence_count == 4
    assert restored.turn.seen_result_hashes == {"content-hash", "call-hash"}
    assert restored.turn.scout_ledger == [{"tool": "grep", "outcome": "hit"}]
    assert restored.turn.steps_since_progress == 2
    assert restored.wind_down_done is True
    assert restored.wind_down_token_mark == 123
    assert restored.turn.loop_blocked_since_progress == 2

def test_checkpoint_and_restore_user_turn_roll_back_per_turn_enforcement():
    # The user-message append transaction snapshots the per-turn enforcement
    # window and rolls it back if the append fails, so a failed turn cannot
    # leave stale brake counters. This locks that contract at the field level
    # (shape-agnostic: only the public checkpoint/restore methods are used).
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    state = session.state
    state.turn.recent_call_hashes = ["h-a"]
    state.turn.reads_since_last_edit = 5
    state.turn.low_yield_since_progress = 2
    state.turn.distinct_evidence_count = 3
    state.turn.seen_result_hashes = {"seen-a"}
    state.turn.scout_ledger = [{"tool": "grep", "outcome": "hit"}]
    state.turn.steps_since_progress = 1
    state.turn.loop_blocked_since_progress = 4
    state.pending_external_user_turn = {
        "turn_id": "queued-turn",
        "status": "queued",
        "content": "retry after restore",
        "message_index": 1,
    }
    # A session-lifetime latch is deliberately NOT part of the per-turn snapshot.
    state.wind_down_done = True

    checkpoint = state.checkpoint_user_turn()

    state.turn.recent_call_hashes.append("h-b")
    state.turn.reads_since_last_edit = 99
    state.turn.low_yield_since_progress = 99
    state.turn.distinct_evidence_count = 99
    state.turn.seen_result_hashes.add("seen-b")
    state.turn.scout_ledger.append({"tool": "read", "outcome": "duplicate"})
    state.turn.steps_since_progress = 99
    state.turn.loop_blocked_since_progress = 99
    state.pending_external_user_turn = None
    state.wind_down_done = False

    state.restore_user_turn(checkpoint)

    assert state.turn.recent_call_hashes == ["h-a"]
    assert state.turn.reads_since_last_edit == 5
    assert state.turn.low_yield_since_progress == 2
    assert state.turn.distinct_evidence_count == 3
    assert state.turn.seen_result_hashes == {"seen-a"}
    assert state.turn.scout_ledger == [{"tool": "grep", "outcome": "hit"}]
    assert state.turn.steps_since_progress == 1
    assert state.turn.loop_blocked_since_progress == 4
    assert state.pending_external_user_turn == {
        "turn_id": "queued-turn",
        "status": "queued",
        "content": "retry after restore",
        "message_index": 1,
    }
    # The lifetime latch is not touched by a per-turn restore.
    assert state.wind_down_done is False

    # The checkpoint is an independent snapshot: mutating restored state and
    # restoring a second time still yields the checkpoint values.
    state.turn.recent_call_hashes.append("h-c")
    state.turn.scout_ledger.append({"tool": "x", "outcome": "y"})
    state.turn.reads_since_last_edit = 42
    state.restore_user_turn(checkpoint)
    assert state.turn.recent_call_hashes == ["h-a"]
    assert state.turn.scout_ledger == [{"tool": "grep", "outcome": "hit"}]
    assert state.turn.reads_since_last_edit == 5
