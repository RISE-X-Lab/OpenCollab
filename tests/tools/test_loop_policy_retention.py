"""Unresolved owners survive the bounded window and fresh operations earn no validation."""
from __future__ import annotations

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application._tool_loop_execution import _LoopBatch
from opencollab.domain.session import SessionState
from opencollab.domain.tool_facts import ToolFacts
from opencollab.domain.tool_loops import LoopState
from opencollab.domain.tools import ToolProcessingResult
from tests.support.tool_execution_test_support import FakeAgent, build_use_case, tool_call


def reserve(state, key, *, count=1, owner="owner", timeout=None, active_keys=None):
    return state.reserve(
        key, recent_same=count, limit=3, workspace_sensitive=True,
        timeout_control=timeout is not None, timeout=timeout,
        tool_call_id=owner, active_keys=active_keys,
    )


def test_fresh_operations_after_edit_cannot_award_validation_progress():
    state = LoopState(edit_seq=7)
    for index in range(5):
        key = f"new-{index}"
        assert reserve(state, key) is None
        assert state.operations[key].last_executed_edit_seq == 7
        assert not state.finish(key, ToolFacts(exit_code=0), tool_call_id="owner", workspace_sensitive=True)
    assert state.edit_seq == 7


def test_old_history_without_operation_state_still_gets_one_validation():
    state = LoopState(edit_seq=7)
    assert reserve(state, "old", count=3) == "validation"
    assert state.finish("old", ToolFacts(exit_code=0), tool_call_id="owner", workspace_sensitive=True)
    assert reserve(state, "old", count=4) == "blocked"


async def test_untracked_custom_edit_does_not_evict_retained_permissions():
    class CustomWriter:
        name = "custom_writer"

        async def execute_with_runtime(self, args, runtime):
            runtime.observations.record_write(completed=True, changed=True, path="owned.txt")
            return "content changed"

    state = SessionState(messages=[])
    keys = [f"retained-{index}" for index in range(200)]
    for index, key in enumerate(keys):
        assert reserve(state.turn.loop_state, key) is None
        if index:
            state.turn.loop_state.finish(key, ToolFacts(exit_code=0), tool_call_id="owner")
    state.turn.recent_call_hashes = list(keys)
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[CustomWriter()]))
    result = await executor.process([tool_call("custom_writer", {}, call_id="custom-edit")])
    result.apply_to(state)
    assert result.current_progress
    assert state.turn.loop_state.edit_seq == 1
    assert set(state.turn.loop_state.operations) == set(keys)
    assert state.turn.loop_state.operations[keys[0]].inflight_tool_call_id == "owner"


def test_pruning_and_restore_keep_out_of_window_unresolved_owners():
    state = LoopState()
    assert reserve(state, "pending", owner="original") is None
    assert reserve(state, "completed") is None
    state.finish("completed", ToolFacts(exit_code=0), tool_call_id="owner")
    state.prune([])
    assert set(state.operations) == {"pending"}
    restored = LoopState.from_dict(state.to_dict(), active_keys=[])
    assert reserve(restored, "pending", owner="replacement") == "inflight"
    assert restored.operations["pending"].inflight_tool_call_id == "original"
    restored.finish("pending", ToolFacts(), tool_call_id="original")
    restored.prune([])
    assert restored.operations == {}


@pytest.mark.parametrize("eager_policy", [False, True])
def test_batch_and_session_window_eviction_preserve_unresolved_owner(eager_policy):
    state = SessionState(messages=[])
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[BashTool()]))
    key = executor.tool_call_hash("bash", {"command": "true"})
    state.turn.recent_call_hashes = [key]
    assert reserve(state.turn.loop_state, key, owner="original") is None
    result = ToolProcessingResult()
    batch = _LoopBatch(executor, result)
    if eager_policy:
        batch._policy()
    for index in range(200):
        batch.append(f"other-{index}")
    batch.publish()
    result.apply_hashes_to(state)
    assert len(state.turn.recent_call_hashes) == 200
    assert key not in state.turn.recent_call_hashes
    assert state.turn.loop_state.operations[key].inflight_tool_call_id == "original"
    count = batch.append(key)
    assert batch.reserve(key, "bash", BashTool(), {"command": "true"}, "replacement", count, 3) == "inflight"
    assert batch.loops.operations[key].inflight_tool_call_id == "original"


def test_full_active_window_cannot_evict_consumed_timeout_recovery():
    state = LoopState()
    assert reserve(state, "pending", owner="unresolved") is None
    for count in (1, 2):
        assert reserve(state, "timed", count=count, timeout=1) is None
        state.finish("timed", ToolFacts(timed_out=True), tool_call_id="owner")
    assert reserve(state, "timed", count=3, timeout=2) == "timeout_recovery"
    state.finish("timed", ToolFacts(timed_out=True), tool_call_id="owner")
    active = {"timed", "new"}
    for index in range(198):
        key = f"completed-{index}"
        active.add(key)
        assert reserve(state, key) is None
        state.finish(key, ToolFacts(exit_code=0), tool_call_id="owner")
    assert len(state.operations) == len(active) == 200
    assert reserve(state, "new", active_keys=active) == "capacity"
    assert state.operations["timed"].timeout_recovery_used
    assert reserve(state, "timed", count=4, timeout=3, active_keys=active) == "blocked"
    state.finish("pending", ToolFacts(), tool_call_id="unresolved")
    state.prune(active)
    assert reserve(state, "new", active_keys=active) is None
    assert len(state.operations) == 200
    assert state.operations["timed"].timeout_recovery_used


def test_two_hundred_unresolved_operations_reject_new_operation_without_dropping_identity():
    state = LoopState()
    for index in range(200):
        assert reserve(state, str(index), owner=f"owner-{index}") is None
    before = state.to_dict()
    assert reserve(state, "new", active_keys=[]) == "capacity"
    assert state.to_dict() == before


def test_restore_prioritizes_unresolved_records_and_rejects_too_many_owners():
    raw = {"operations": {
        **{f"completed-{index}": {} for index in range(200)},
        "pending": {"inflight_tool_call_id": "original"},
    }}
    restored = LoopState.from_dict(raw)
    assert len(restored.operations) == 200
    assert restored.operations["pending"].inflight_tool_call_id == "original"
    raw["operations"] = {str(index): {"inflight_tool_call_id": f"owner-{index}"} for index in range(201)}
    with pytest.raises(ValueError, match="more than 200 unresolved"):
        LoopState.from_dict(raw, active_keys=[])


async def test_fresh_noop_commands_cannot_reset_known_blocked_batches_after_edit(tmp_path):
    (tmp_path / "f.txt").write_text("before")
    env = LocalEnvironment(str(tmp_path))
    state = SessionState(messages=[])
    executor, _ = build_use_case(
        state=state, agent=FakeAgent(tools=[BashTool(), FileWriteTool()]), environment=env,
    )

    async def step(*calls):
        state.advance_step()
        result = await executor.process(list(calls))
        result.apply_to(state)
        return result

    def read(owner):
        return tool_call("bash", {"command": "cat f.txt"}, call_id=owner)

    try:
        for index in range(2):
            await step(read(f"prime-{index}"))
        await step(tool_call(
            "file_write", {"path": "f.txt", "mode": "create", "content": "after", "overwrite": True},
            call_id="edit",
        ))
        await step(read("validation"))
        for index in range(4):
            distinct = state.turn.distinct_evidence_count
            result = await step(
                read(f"blocked-{index}"),
                tool_call("bash", {"command": f"true # fresh-{index}"}, call_id=f"fresh-{index}"),
            )
            assert len(result.loop_detections) == 1
            assert not result.current_progress
            assert state.turn.loop_state.edit_seq == 1
            if index:
                assert state.turn.distinct_evidence_count == distinct
                assert state.turn.loop_state.blocked_rounds == index
        assert state.turn.loop_state.blocked_rounds == 3
    finally:
        await env.cleanup()
