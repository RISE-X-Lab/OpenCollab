"""Repeat permissions are finite, ordered, bounded and durable."""

import math

import pytest

from opencollab.domain.session import SessionState
from opencollab.domain.tool_facts import ToolFacts
from opencollab.domain.tool_loops import LoopState


def reserve(state, key="test", *, count=1, sensitive=True, timeout=None, call_id="call"):
    return state.reserve(
        key,
        recent_same=count,
        limit=3,
        workspace_sensitive=sensitive,
        timeout_control=timeout is not None,
        timeout=timeout,
        tool_call_id=call_id,
    )


def edit(state, *, call_id="edit", facts=None):
    assert reserve(state, "writer", sensitive=False, call_id=call_id) is None
    return state.finish(
        "writer",
        facts or ToolFacts(observed=True, write_completed=True, content_changed=True),
        tool_call_id=call_id,
    )


def test_ordinary_calls_keep_the_original_threshold():
    state = LoopState()
    for count in (1, 2):
        assert reserve(state, count=count) is None
        assert not state.finish("test", ToolFacts(exit_code=0), tool_call_id="call")
    assert reserve(state, count=3) == "blocked"
    assert not state.finish("test", ToolFacts(content_changed=True), tool_call_id="call")
    assert state.edit_seq == 0


def test_confirmed_edit_allows_only_one_old_command_and_another_edit_renews_it():
    state = LoopState()
    assert reserve(state) is None
    state.finish("test", ToolFacts(exit_code=0), tool_call_id="call")
    assert edit(state)
    assert reserve(state, count=10) == "validation"
    assert state.finish("test", ToolFacts(exit_code=0), tool_call_id="call", workspace_sensitive=True)
    assert reserve(state, count=11) == "blocked"
    assert edit(state, call_id="second-edit")
    assert reserve(state, count=12) == "validation"


def test_normal_execution_after_edit_already_consumes_the_edit_opportunity():
    state = LoopState()
    assert edit(state)
    assert reserve(state, count=2) is None
    state.finish("test", ToolFacts(exit_code=0), tool_call_id="call", workspace_sensitive=True)
    assert reserve(state, count=3) == "blocked"


def test_write_tools_and_independent_operations_never_receive_edit_permissions():
    state = LoopState()
    assert edit(state)
    assert reserve(state, "writer", count=3, sensitive=False) == "blocked"
    assert reserve(state, "independent", count=3, sensitive=False) == "blocked"


@pytest.mark.parametrize(
    "facts",
    [ToolFacts(), ToolFacts(write_completed=True, content_changed=False), ToolFacts(exit_code=1)],
)
def test_unknown_unchanged_and_failed_writes_do_not_advance_edit_sequence(facts):
    state = LoopState()
    assert not edit(state, facts=facts)
    assert state.edit_seq == 0
    assert reserve(state, count=3) == "blocked"


def test_partial_write_followed_by_failure_is_still_a_confirmed_edit():
    state = LoopState()
    assert edit(state, facts=ToolFacts(exit_code=1, write_completed=False, content_changed=True))
    assert state.edit_seq == 1
    assert reserve(state, count=3) == "validation"


def test_a_mutating_bash_cannot_renew_its_own_permission():
    state = LoopState()
    for count in (1, 2):
        assert reserve(state, count=count) is None
        assert state.finish("test", ToolFacts(exit_code=0, content_changed=True), tool_call_id="call")
    assert state.edit_seq == 2
    assert reserve(state, count=3) == "blocked"


def test_edit_and_two_same_commands_in_one_batch_only_dispatch_once():
    state = LoopState()
    assert edit(state)
    assert reserve(state, count=8, call_id="first") == "validation"
    state.finish("test", ToolFacts(exit_code=0), tool_call_id="first", workspace_sensitive=True)
    assert reserve(state, count=9, call_id="second") == "blocked"


def timeout_twice(state):
    for count in (1, 2):
        assert reserve(state, count=count, timeout=60) is None
        state.finish("test", ToolFacts(timed_out=True), tool_call_id="call")


def test_real_timeouts_offer_one_strictly_larger_recovery():
    state = LoopState()
    timeout_twice(state)
    assert reserve(state, count=3, timeout=60) == "blocked"
    assert reserve(state, count=4, timeout=120) == "timeout_recovery"
    assert state.finish("test", ToolFacts(exit_code=0, timed_out=False), tool_call_id="call")
    assert reserve(state, count=5, timeout=240) == "blocked"
    assert state.operations["test"].max_attempted_timeout == 120


def test_combined_permissions_are_spent_by_the_same_dispatch():
    state = LoopState()
    timeout_twice(state)
    assert edit(state)
    assert reserve(state, count=3, timeout=120) == "validation+timeout_recovery"
    state.finish("test", ToolFacts(timed_out=True), tool_call_id="call")
    assert reserve(state, count=4, timeout=240) == "blocked"


def test_unknown_timeout_text_and_normal_failure_never_offer_timeout_recovery():
    for facts in (ToolFacts(), ToolFacts(exit_code=1, timed_out=False)):
        state = LoopState()
        assert reserve(state, timeout=60) is None
        state.finish("test", facts, "output-says-timeout", tool_call_id="call")
        assert reserve(state, count=3, timeout=120) == "blocked"


@pytest.mark.parametrize("timeout", [True, 0, -1, math.inf, math.nan, "120"])
def test_invalid_timeout_does_not_issue_a_recovery(timeout):
    state = LoopState()
    timeout_twice(state)
    assert reserve(state, count=3, timeout=timeout) == "blocked"
    assert state.operations["test"].max_attempted_timeout == 60


def test_inflight_and_late_results_cannot_alter_current_execution():
    state = LoopState()
    assert reserve(state, timeout=60, call_id="owner") is None
    assert reserve(state, timeout=120, call_id="other") == "inflight"
    assert state.operations["test"].max_attempted_timeout == 60
    assert not state.finish("test", ToolFacts(content_changed=True), tool_call_id="other")
    assert state.edit_seq == 0
    assert state.operations["test"].inflight_tool_call_id == "owner"
    state.finish("test", ToolFacts(exit_code=0), tool_call_id="owner")
    assert not state.finish("test", ToolFacts(content_changed=True), tool_call_id="owner")
    assert state.edit_seq == 0


def test_restore_keeps_consumed_inflight_permission_until_explicit_resolution():
    state = LoopState()
    assert edit(state)
    assert reserve(state, count=3, call_id="reserved") == "validation"
    restored = LoopState.from_dict(state.to_dict(), active_keys={"writer", "test"})
    assert reserve(restored, count=4, call_id="new") == "inflight"
    restored.finish("test", ToolFacts(), tool_call_id="reserved")
    assert reserve(restored, count=5, call_id="new") == "blocked"


def test_progress_requires_confirmed_state_improvement():
    state = LoopState()
    assert reserve(state) is None
    assert not state.finish("test", ToolFacts(), "different-output", tool_call_id="call")
    assert reserve(state, count=2) is None
    assert not state.finish("test", ToolFacts(exit_code=0), "new-output", tool_call_id="call")
    assert edit(state)
    assert reserve(state, count=3) == "validation"
    assert state.finish("test", ToolFacts(), "readback", tool_call_id="call", completed=True, workspace_sensitive=True)


def test_completed_validation_is_finite_even_when_later_output_keeps_changing():
    state = LoopState()
    assert reserve(state) is None
    assert not state.finish("test", ToolFacts(exit_code=0), "old-result", tool_call_id="call")
    assert edit(state)
    assert reserve(state) is None
    assert state.finish("test", ToolFacts(exit_code=0), "same-result", tool_call_id="call", workspace_sensitive=True)
    assert reserve(state, count=2) is None
    assert not state.finish(
        "test", ToolFacts(exit_code=0), "random-next-result", tool_call_id="call", workspace_sensitive=True
    )
    assert reserve(state, count=3) == "blocked"


def test_failed_execution_followed_by_success_is_finite_progress():
    state = LoopState()
    assert reserve(state) is None
    assert not state.finish("test", ToolFacts(exit_code=1), tool_call_id="call")
    assert reserve(state, count=2) is None
    assert state.finish("test", ToolFacts(exit_code=0), tool_call_id="call")
    assert reserve(state, count=3) == "blocked"


def test_operation_state_remains_bounded_and_prunes_only_absent_keys():
    state = LoopState()
    for index in range(250):
        key = f"operation-{index}"
        assert reserve(state, key) is None
        state.finish(key, ToolFacts(), tool_call_id="call")
        assert len(state.operations) <= 200
    state.prune(["operation-249", "operation-249"])
    assert list(state.operations) == ["operation-249"]


def test_clone_and_snapshot_round_trip_do_not_share_mutable_state():
    state = LoopState(blocked_rounds=2, last_counted_step=9)
    timeout_twice(state)
    assert edit(state)
    cloned = state.clone()
    restored = LoopState.from_dict(state.to_dict())
    assert cloned.to_dict() == restored.to_dict() == state.to_dict()
    assert cloned.operations["test"] is state.operations["test"]
    assert reserve(cloned, count=3, timeout=120) == "validation+timeout_recovery"
    assert cloned.operations["test"] is not state.operations["test"]
    assert not state.operations["test"].timeout_recovery_used
    assert state.operations["test"].inflight_tool_call_id is None


@pytest.mark.parametrize("old_count, rounds", [(0, 0), (1, 1), (3, 1), (99, 1)])
def test_legacy_call_counts_migrate_to_one_known_round(old_count, rounds):
    state = LoopState.from_dict(None, legacy_blocked_calls=old_count)
    assert state.blocked_rounds == rounds
    assert state.edit_seq == 0
    assert state.operations == {}
    assert state.last_counted_step == -1


def test_round_trip_and_pruning_filter_malformed_snapshot_fields():
    state = LoopState.from_dict({
        "edit_seq": 2,
        "blocked_rounds": True,
        "operations": {
            "active": {"last_executed_edit_seq": 99, "max_attempted_timeout": math.inf},
            "old": {"timeout_recovery_used": True},
        },
    }, active_keys={"active"})
    assert state.blocked_rounds == 0
    assert list(state.operations) == ["active"]
    assert state.operations["active"].last_executed_edit_seq == 2
    assert state.operations["active"].max_attempted_timeout == 0


def test_user_turn_checkpoint_rolls_back_and_new_turn_resets_policy():
    session = SessionState(messages=[])
    assert edit(session.turn.loop_state)
    session.turn.loop_state.blocked_rounds = 2
    session.turn.write_effect_unknown = True
    checkpoint = session.checkpoint_user_turn()
    session.turn.loop_state.edit_seq = 10
    session.turn.loop_state.blocked_rounds = 3
    session.turn.last_progress_unknown = True
    session.restore_user_turn(checkpoint)
    assert session.turn.loop_state.edit_seq == 1
    assert session.turn.loop_state.blocked_rounds == 2
    assert not session.turn.last_progress_unknown
    session.reset_for_user_turn()
    assert session.turn.loop_state == LoopState()
    assert not session.turn.write_effect_unknown
