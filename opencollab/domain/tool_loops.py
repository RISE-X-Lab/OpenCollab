"""Bounded repeat-call permissions backed by observed execution facts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from opencollab.domain.tool_facts import ToolFacts

_MAX_OPERATIONS = 200
_ANONYMOUS_CALL = "<anonymous>"
_EXIT_KINDS = frozenset({"unknown", "completed", "failed", "timeout"})


def _nonnegative_int(value: Any, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


def _positive_timeout(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


@dataclass(frozen=True)
class _OperationState:
    last_executed_edit_seq: int = 0
    validation_reserved_edit_seq: int = 0
    last_exit_kind: str = "unknown"
    max_attempted_timeout: float = 0.0
    timeout_recovery_used: bool = False
    last_result_key: str | None = None
    inflight_tool_call_id: str | None = None


@dataclass
class LoopState:
    """Keep at most one edit permission and one timeout recovery per operation.

    The caller supplies existing operation keys and owns the 200-call window.
    ``prune`` must follow window changes. This record performs no I/O; a runner
    can persist a consumed exceptional permission before dispatching its tool.
    """

    edit_seq: int = 0
    blocked_rounds: int = 0
    last_counted_step: int = -1
    operations: dict[str, _OperationState] = field(default_factory=dict)

    def clone(self) -> LoopState:
        return replace(self, operations=dict(self.operations))

    def prune(self, active_keys: Iterable[str]) -> None:
        active = set(active_keys)
        for key in list(self.operations):
            if key not in active and self.operations[key].inflight_tool_call_id is None:
                del self.operations[key]

    def reserve(
        self,
        key: str,
        *,
        recent_same: int,
        limit: int,
        workspace_sensitive: bool,
        timeout_control: bool = False,
        timeout: float | None = None,
        tool_call_id: str | None = None,
        active_keys: Iterable[str] | None = None,
    ) -> str | None:
        """Reserve a dispatch, returning its exception reason or rejection.

        ``None`` allows an ordinary dispatch. ``validation``,
        ``timeout_recovery`` and their ``+`` combination allow an exceptional
        dispatch. ``blocked``, ``inflight`` and ``capacity`` reject it without spending a new
        permission or changing its maximum attempted timeout.
        """
        operation = self.operations.get(key)
        if operation is not None and operation.inflight_tool_call_id is not None:
            return "inflight"
        if operation is None:
            if len(self.operations) >= _MAX_OPERATIONS:
                active = set(active_keys) if active_keys is not None else None
                evict = next(
                    (
                        entry for entry, value in self.operations.items()
                        if value.inflight_tool_call_id is None and (active is None or entry not in active)
                    ),
                    None,
                )
                if evict is None:
                    return "capacity"
                del self.operations[evict]
            initial_edit_seq = self.edit_seq if recent_same <= 1 else 0
            operation = _OperationState(
                last_executed_edit_seq=initial_edit_seq,
                validation_reserved_edit_seq=initial_edit_seq,
            )
            self.operations[key] = operation

        wait = _positive_timeout(timeout) if timeout_control else None
        validation = (
            workspace_sensitive
            and self.edit_seq > max(operation.last_executed_edit_seq, operation.validation_reserved_edit_seq)
        )
        recovery = (
            wait is not None
            and operation.last_exit_kind == "timeout"
            and wait > operation.max_attempted_timeout
            and not operation.timeout_recovery_used
        )
        exceptional = recent_same >= limit
        if exceptional and not (validation or recovery):
            return "blocked"

        self.operations[key] = replace(
            operation,
            inflight_tool_call_id=tool_call_id if isinstance(tool_call_id, str) else _ANONYMOUS_CALL,
            validation_reserved_edit_seq=self.edit_seq,
            max_attempted_timeout=(
                max(operation.max_attempted_timeout, wait)
                if wait is not None else operation.max_attempted_timeout
            ),
            timeout_recovery_used=operation.timeout_recovery_used or (exceptional and recovery),
        )
        if not exceptional:
            return None
        if validation and recovery:
            return "validation+timeout_recovery"
        return "validation" if validation else "timeout_recovery"

    def finish(
        self,
        key: str,
        facts: ToolFacts,
        result_key: str | None = None,
        *,
        tool_call_id: str | None = None,
        workspace_sensitive: bool = False,
        completed: bool = False,
    ) -> bool:
        """Finish the owning invocation and return its finite progress signal.

        An unknown completion also releases an interrupted invocation after the
        runner has supplied its explicit failure result. Consumed permissions
        stay spent. A mismatched or duplicate completion contributes no facts.
        Completion of one workspace-dependent validation after an edit counts
        once even when its output is unchanged. Later text differences do not
        renew this progress signal.
        """
        operation = self.operations.get(key)
        owner = tool_call_id if isinstance(tool_call_id, str) else _ANONYMOUS_CALL
        if operation is None or operation.inflight_tool_call_id != owner:
            return False

        previous_kind = operation.last_exit_kind
        if facts.timed_out is True:
            exit_kind = "timeout"
        elif type(facts.exit_code) is int:
            exit_kind = "completed" if facts.exit_code == 0 else "failed"
        elif facts.write_completed is False:
            exit_kind = "failed"
        elif facts.write_completed is True or completed:
            exit_kind = "completed"
        else:
            exit_kind = "unknown"

        changed = facts.content_changed is True
        validated_edit = (
            workspace_sensitive
            and self.edit_seq > operation.last_executed_edit_seq
            and exit_kind == "completed"
        )
        recovered_execution = previous_kind in {"timeout", "failed"} and exit_kind == "completed"
        if changed:
            self.edit_seq += 1
        self.operations[key] = replace(
            operation,
            last_executed_edit_seq=self.edit_seq,
            validation_reserved_edit_seq=max(operation.validation_reserved_edit_seq, self.edit_seq),
            last_exit_kind=exit_kind,
            last_result_key=result_key if isinstance(result_key, str) else None,
            inflight_tool_call_id=None,
        )
        return changed or validated_edit or recovered_execution

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "edit_seq": self.edit_seq,
            "blocked_rounds": self.blocked_rounds,
            "last_counted_step": self.last_counted_step,
            "operations": {
                key: {
                    "last_executed_edit_seq": value.last_executed_edit_seq,
                    "validation_reserved_edit_seq": value.validation_reserved_edit_seq,
                    "last_exit_kind": value.last_exit_kind,
                    "max_attempted_timeout": value.max_attempted_timeout,
                    "timeout_recovery_used": value.timeout_recovery_used,
                    "last_result_key": value.last_result_key,
                    "inflight_tool_call_id": value.inflight_tool_call_id,
                }
                for key, value in self.operations.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        raw: Any,
        *,
        legacy_blocked_calls: int = 0,
        active_keys: Iterable[str] | None = None,
    ) -> LoopState:
        """Restore bounded state, retaining unresolved owners before completed records.

        Oversized snapshots may trim completed records. More than 200 unresolved
        owners is invalid because restoration cannot safely discard an owner.
        """
        if not isinstance(raw, Mapping):
            return cls(blocked_rounds=int(_nonnegative_int(legacy_blocked_calls) > 0))
        state = cls(
            edit_seq=_nonnegative_int(raw.get("edit_seq")),
            blocked_rounds=_nonnegative_int(raw.get("blocked_rounds")),
            last_counted_step=_nonnegative_int(raw.get("last_counted_step"), -1),
        )
        active = set(active_keys) if active_keys is not None else None
        operations = raw.get("operations")
        if isinstance(operations, Mapping):
            for key, value in operations.items():
                inflight = value.get("inflight_tool_call_id") if isinstance(value, Mapping) else None
                if (
                    not isinstance(key, str)
                    or not isinstance(value, Mapping)
                    or (active is not None and key not in active and not isinstance(inflight, str))
                ):
                    continue
                executed = min(_nonnegative_int(value.get("last_executed_edit_seq")), state.edit_seq)
                reserved = min(_nonnegative_int(value.get("validation_reserved_edit_seq")), state.edit_seq)
                kind = value.get("last_exit_kind")
                result = value.get("last_result_key")
                if len(state.operations) >= _MAX_OPERATIONS:
                    evict = next(
                        (entry for entry, item in state.operations.items() if item.inflight_tool_call_id is None),
                        None,
                    )
                    if evict is None:
                        if isinstance(inflight, str):
                            raise ValueError("loop snapshot contains more than 200 unresolved operations")
                        continue
                    del state.operations[evict]
                state.operations[key] = _OperationState(
                    last_executed_edit_seq=executed,
                    validation_reserved_edit_seq=reserved,
                    last_exit_kind=kind if isinstance(kind, str) and kind in _EXIT_KINDS else "unknown",
                    max_attempted_timeout=_positive_timeout(value.get("max_attempted_timeout")) or 0.0,
                    timeout_recovery_used=value.get("timeout_recovery_used") is True,
                    last_result_key=result if isinstance(result, str) else None,
                    inflight_tool_call_id=inflight if isinstance(inflight, str) else None,
                )
        return state


__all__ = ["LoopState"]
