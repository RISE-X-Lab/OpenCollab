"""Apply bounded repeat permissions within one ordered tool batch."""

from __future__ import annotations

import math
from typing import Any

from opencollab.domain.session import SessionState
from opencollab.domain.tool_facts import ToolFacts
from opencollab.domain.tools import MAX_CALL_HASH_WINDOW, ToolProcessingResult

_WORKSPACE_READS = frozenset({"file_read", "grep", "git_diff"})


def _workspace_sensitive(name: str, tool: Any) -> bool:
    return name in _WORKSPACE_READS or getattr(tool, "loop_workspace_observer", False) is True


def _controlled_timeout(tool: Any, args: dict) -> float | None:
    if getattr(tool, "loop_timeout_is_control", False) is not True:
        return None
    value = args.get("timeout", getattr(tool, "loop_default_timeout", None))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        timeout = float(value)
    except (ValueError, OverflowError):
        return None
    return timeout if math.isfinite(timeout) and timeout > 0 else None


def _apply_completed_prefix_progress(state: SessionState, result: ToolProcessingResult) -> bool:
    """Publish confirmed prefix progress without folding the unfinished model step."""
    prefix_progress = result.write_succeeded or result.current_progress
    if not prefix_progress:
        seen = state.turn.seen_result_hashes
        prefix_seen: set[str] = set()
        for content_key, call_key, low_yield in result.evidence_signals:
            if (
                not low_yield
                and content_key not in seen and call_key not in seen
                and content_key not in prefix_seen and call_key not in prefix_seen
            ):
                prefix_progress = True
                break
            prefix_seen.update((content_key, call_key))
    if prefix_progress:
        turn = state.turn
        turn.loop_state.blocked_rounds = 0
        turn.loop_blocked_since_progress = 0
        turn.steps_since_progress = 0
        turn.last_progress_unknown = False
    return prefix_progress


class _LoopBatch:
    def __init__(self, executor: Any, result: ToolProcessingResult) -> None:
        self.executor = executor
        self.result = result
        self.recent = list(executor.state.turn.recent_call_hashes)
        self.loops = None
        self.evicted: set[str] = set()
        self.checkpointed = False

    def append(self, key: str) -> int:
        self.recent.append(key)
        if len(self.recent) > MAX_CALL_HASH_WINDOW:
            removed = self.recent.pop(0)
            if removed not in self.recent:
                self.evicted.add(removed)
                if self.loops is not None:
                    operation = self.loops.operations.get(removed)
                    if operation is not None and operation.inflight_tool_call_id is None:
                        self.loops.operations.pop(removed, None)
        self.result.recent_hash_updates.append(key)
        return self.recent.count(key)

    def _policy(self):
        if self.loops is None:
            self.loops = self.executor.state.turn.loop_state.clone()
            for key in self.evicted:
                operation = self.loops.operations.get(key)
                if operation is not None and operation.inflight_tool_call_id is None:
                    self.loops.operations.pop(key, None)
        return self.loops

    def reserve(self, key: str, name: str, tool: Any, args: dict, tool_id: str, count: int, limit: int):
        current = self.loops if self.loops is not None else self.executor.state.turn.loop_state
        needs_policy = (
            key in current.operations
            or name in {"file_write", "apply_patch"}
            or getattr(tool, "loop_timeout_is_control", False) is True
            or current.edit_seq > 0 and _workspace_sensitive(name, tool)
        )
        if not needs_policy:
            return "blocked" if count >= limit else None
        timeout = _controlled_timeout(tool, args)
        if timeout is not None:
            deadline = self.executor.tool_execution_timeout(tool, args)
            if deadline is not None:
                timeout = min(timeout, deadline)
        return self._policy().reserve(
            key,
            recent_same=count,
            limit=limit,
            workspace_sensitive=_workspace_sensitive(name, tool),
            timeout_control=getattr(tool, "loop_timeout_is_control", False) is True,
            timeout=timeout,
            tool_call_id=tool_id,
            active_keys=self.recent,
        )

    def _apply_completed_prefix(self) -> None:
        # Publish the ordered prefix before the Session freezes its snapshot.
        # The result tracks that prefix so its final application is idempotent.
        self.publish()
        self.result.apply_hashes_to(self.executor.state)
        self.result.apply_read_write_counter_to(self.executor.state)
        if _apply_completed_prefix_progress(self.executor.state, self.result):
            self._policy().blocked_rounds = 0
        # Evidence and the model-step marker still fold once when the batch ends.

    async def checkpoint(self) -> None:
        self.checkpointed = True
        self._apply_completed_prefix()
        callback = getattr(self.executor, "loop_reservation_checkpoint", None)
        if callback is not None:
            await callback(self.result.messages_to_append)

    def sync_completed_prefix(self) -> None:
        if self.checkpointed:
            self._apply_completed_prefix()

    def finish(
        self, key: str, name: str, tool: Any, tool_id: str, facts: ToolFacts,
        result_key: str, *, completed: bool,
    ) -> None:
        if self.loops is None and facts.content_changed is not True:
            return
        policy = self._policy()
        if key not in policy.operations and facts.content_changed is True:
            # An untracked custom tool can report a completed edit without
            # evicting another operation's retained recovery permission.
            policy.edit_seq += 1
            self.result.current_progress = True
            return
        self.result.current_progress |= policy.finish(
            key, facts, result_key,
            tool_call_id=tool_id,
            workspace_sensitive=_workspace_sensitive(name, tool),
            completed=completed,
        )

    def publish(self) -> None:
        if self.loops is not None:
            self.result.loop_state_update = self.loops
