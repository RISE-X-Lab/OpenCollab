from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import (
    EnvironmentPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SessionStorePort,
    SnapshotStorePort,
    TracePort,
)
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState

if TYPE_CHECKING:
    from opencollab.application.scheduler import LaunchSpec


@dataclass
class SessionRuntime:
    """Pre-built collaborators a ``Session`` facade keeps as attributes.

    Not frozen: ``Session`` reassigns a few attributes during lifecycle
    operations (e.g. setting permission policy propagates through the
    tool processor); keeping this mutable keeps the facade simple.
    """

    state: SessionState
    event_bus: EventBus
    llm: LLMPort
    store: SessionStorePort
    tool_execution: ToolExecutionUseCase
    runner: SessionRunUseCase
    auto_save_path: str | None
    auto_save_subscriber: AutoSaveSubscriber | None = None


class Session:
    """Public facade for a stateful agent session.

    This application-layer facade owns session lifecycle state access but not
    concrete collaborator construction. Callers must pass a pre-built
    ``SessionRuntime`` from the composition root.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        runtime: SessionRuntime,
        env: EnvironmentPort | None = None,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 1_000_000,
        max_steps: int = 100,
        auto_save_path: str | None = None,
        permission_policy: PermissionPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
    ):
        self.agent = agent
        self.env = env
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self._permission_policy = permission_policy
        self._safety_policy = safety_policy
        self._auto_save_path = auto_save_path
        self._launch_applied = False

        # Adopt the runtime's collaborators as Session attributes so the
        # public surface stays exactly what it used to be.
        self.state = runtime.state
        self.event_bus = runtime.event_bus
        self._llm = runtime.llm
        self.store = runtime.store
        self.tool_execution = runtime.tool_execution
        self.runner = runtime.runner
        self._auto_save_subscriber = runtime.auto_save_subscriber
        # The env attribute mirrors the runtime's tool_execution env so
        # downstream readers (snapshot, characterization tests) still see
        # the same Environment instance.
        if env is None:
            self.env = self.tool_execution.environment

    @property
    def permission_policy(self) -> PermissionPort | None:
        return self._permission_policy

    @permission_policy.setter
    def permission_policy(self, value: PermissionPort | None) -> None:
        self._permission_policy = value
        if hasattr(self, "tool_execution"):
            self.tool_execution.permission_policy = value

    @property
    def auto_save_path(self) -> str | None:
        return self._auto_save_path

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Background subscriber work that must finish before session teardown."""
        owned = {
            *self.event_bus.pending_tasks,
            *self.runner.pending_cleanup_tasks,
            *getattr(self.tool_execution, "pending_cleanup_tasks", ()),
        }
        return tuple(task for task in owned if not task.done())

    @property
    def persistence_errors(self) -> tuple[Exception, ...]:
        """Sticky subscriber persistence failures visible to run boundaries."""
        errors: list[Exception] = []
        for subscriber in self.event_bus.subscribers:
            error = getattr(subscriber, "last_error", None)
            if isinstance(error, Exception):
                errors.append(error)
        return tuple(errors)

    @property
    def messages(self) -> list[dict]:
        return self.state.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.state.replace_messages(value)

    @property
    def used_tokens(self) -> int:
        return self.state.used_tokens

    @used_tokens.setter
    def used_tokens(self, value: int) -> None:
        self.state.set_used_tokens(value)

    @property
    def step_count(self) -> int:
        return self.state.step_count

    @step_count.setter
    def step_count(self, value: int) -> None:
        self.state.set_step_count(value)

    @property
    def markup_recovered(self) -> int:
        return self.state.markup_recovered

    @property
    def is_done(self) -> bool:
        return self.state.is_done

    @is_done.setter
    def is_done(self, value: bool) -> None:
        if value:
            self.state.mark_done()
        else:
            self.state.clear_done()

    @property
    def _recent_call_hashes(self) -> list[str]:
        return self.state.recent_call_hashes

    @_recent_call_hashes.setter
    def _recent_call_hashes(self, value: list[str]) -> None:
        self.state.replace_recent_tool_hashes(value)

    @property
    def phase(self) -> SessionPhase:
        return self.state.phase

    @phase.setter
    def phase(self, value: SessionPhase) -> None:
        self.state.set_phase(value)

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        return await self.runner.run_loop(cancel_event)

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})
        self.state.reset_for_user_turn()
        await self.event_bus.emit(SessionEvent(type="user_message_appended"))

    def apply_launch(self, launch: "LaunchSpec") -> None:
        """Apply launch-time persistence as a one-shot lifecycle step.

        Resumes from ``launch.session_file`` if it exists, otherwise seeds the
        auto-save file. Idempotent: a second call is a no-op, so live message
        state is never clobbered by a re-resume and the seed file is not
        re-truncated. The ongoing per-event ``AutoSaveSubscriber`` (wired at
        construction) is a separate concern and unaffected.
        """
        if self._launch_applied:
            return
        self._launch_applied = True
        if launch.session_file and os.path.exists(launch.session_file):
            self.restore(launch.session_file)
        elif launch.auto_save_path:
            self.save(launch.auto_save_path)

    def restore(self, path: str) -> None:
        """Restore a complete snapshot while accepting legacy message-only stores."""
        if isinstance(self.store, SnapshotStorePort):
            snapshot = self.store.load_snapshot(path, self.agent.system_prompt)
        else:
            snapshot = {
                "messages": self.store.load_messages(path, self.agent.system_prompt)
            }

        self.messages = list(snapshot.get("messages", []))
        pending_messages = snapshot.get("pending_messages", [])
        self.state.pending_user_messages = [
            dict(message) for message in pending_messages if isinstance(message, dict)
        ] if isinstance(pending_messages, list) else []
        self.state.aid = _snapshot_int(snapshot.get("aid"), default=self.state.aid)
        raw_state = snapshot.get("session_state")
        if not isinstance(raw_state, dict):
            return

        self.state.set_used_tokens(_snapshot_nonnegative_int(raw_state.get("used_tokens")))
        self.state.set_context_tokens(_snapshot_nonnegative_int(raw_state.get("context_tokens")))
        self.state.set_step_count(_snapshot_nonnegative_int(raw_state.get("step_count")))
        self.state.markup_recovered = _snapshot_nonnegative_int(
            raw_state.get("markup_recovered")
        )
        hashes = raw_state.get("recent_call_hashes")
        self.state.replace_recent_tool_hashes(
            [str(value) for value in hashes] if isinstance(hashes, list) else []
        )
        self.state.reads_since_last_edit = _snapshot_nonnegative_int(
            raw_state.get("reads_since_last_edit")
        )
        self.state.low_yield_since_progress = _snapshot_nonnegative_int(
            raw_state.get("low_yield_since_progress")
        )
        self.state.distinct_evidence_count = _snapshot_nonnegative_int(
            raw_state.get("distinct_evidence_count")
        )
        seen_hashes = raw_state.get("seen_result_hashes")
        self.state._seen_result_hashes = (
            {str(value) for value in seen_hashes}
            if isinstance(seen_hashes, list)
            else set()
        )
        ledger = raw_state.get("scout_ledger")
        self.state.scout_ledger = (
            [dict(card) for card in ledger if isinstance(card, dict)]
            if isinstance(ledger, list)
            else []
        )
        self.state.steps_since_progress = _snapshot_nonnegative_int(
            raw_state.get("steps_since_progress")
        )
        self.state.wind_down_done = bool(raw_state.get("wind_down_done", False))
        self.state.wind_down_token_mark = _snapshot_nonnegative_int(
            raw_state.get("wind_down_token_mark")
        )
        self.state.forced_unsatisfied = bool(raw_state.get("forced_unsatisfied", False))
        self.state.loop_blocked_since_progress = _snapshot_nonnegative_int(
            raw_state.get("loop_blocked_since_progress")
        )
        self.state.extension_offered = bool(raw_state.get("extension_offered", False))
        self.state.extensions_granted = _snapshot_nonnegative_int(
            raw_state.get("extensions_granted")
        )
        reasons = raw_state.get("extension_reasons")
        self.state.extension_reasons = (
            [str(value) for value in reasons] if isinstance(reasons, list) else []
        )
        self.state.pending_events.clear()
        rows = raw_state.get("pending_events", [])
        if isinstance(rows, list):
            for value in rows:
                row = _restore_pending_row(value)
                if row is not None:
                    self.state.pending_events.add(row)

        phase_value = raw_state.get("phase", SessionPhase.IDLE.value)
        try:
            phase = SessionPhase(str(phase_value))
        except ValueError:
            phase = SessionPhase.IDLE
        if phase is SessionPhase.AWAITING_EVENTS:
            self._complete_missing_pending_rows()
        # In-flight provider/tool phases depend on process-local coroutines that
        # cannot survive restart. AWAITING_EVENTS is recoverable because pending
        # child rows above are converted to explicit FAILED tool results.
        recoverable = {SessionPhase.AWAITING_EVENTS, *[p for p in SessionPhase if p.is_terminal()]}
        self.state.set_phase(phase if phase in recoverable else SessionPhase.IDLE)
        if phase is not SessionPhase.AWAITING_EVENTS:
            self._append_restore_results_for_open_tool_calls()
            # Rows from an interrupted non-awaiting phase have no live producer
            # after process restart. The explicit tool results above close the
            # provider protocol; keeping the stale sidecar rows would make the
            # scheduler's quiescence check wait forever after the resumed turn.
            self.state.pending_events.clear()
        self.state.terminal_reason = (
            str(raw_state["terminal_reason"])
            if raw_state.get("terminal_reason") is not None
            else None
        )
        if self.state.phase is SessionPhase.AWAITING_EVENTS and self.state.pending_events.is_empty():
            self.state.set_phase(SessionPhase.IDLE)
            self._append_restore_results_for_open_tool_calls()

    def _open_tool_call_ids(self) -> list[str]:
        # Pair calls and results in transcript order. Tool-call ids are expected
        # to be unique, but several OpenAI-compatible providers reuse short ids
        # such as ``call_1`` on later turns. A global "answered ids" set would
        # let an old result incorrectly close a newer interrupted call.
        open_ids: list[str] = []
        for message in self.state.messages:
            if message.get("role") == "tool":
                tool_call_id = message.get("tool_call_id")
                if tool_call_id is None:
                    continue
                try:
                    open_ids.remove(str(tool_call_id))
                except ValueError:
                    pass
                continue
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict) or not call.get("id"):
                    continue
                open_ids.append(str(call["id"]))
        return open_ids

    def _complete_missing_pending_rows(self) -> None:
        existing = set(self.state.pending_events.rows)
        for order, tool_call_id in enumerate(self._open_tool_call_ids()):
            if tool_call_id in existing:
                continue
            error = "deferred child state missing during session restore"
            self.state.pending_events.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.CHILD_AGENT,
                    order=order,
                    status=RowStatus.FAILED,
                    result=error,
                    error=error,
                )
            )

    def _append_restore_results_for_open_tool_calls(self) -> None:
        """Close assistant tool calls whose process-local execution was lost."""
        for tool_call_id in self._open_tool_call_ids():
            self.state.append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "Tool execution interrupted by session restore.",
                }
            )

    def save(self, path: str) -> None:
        messages, meta = self._snapshot_for_save()
        self.store.save(path, messages, meta=meta)

    def _snapshot_for_save(self) -> tuple[list[dict], dict]:
        """Freeze one internally consistent payload before persistence starts."""
        meta = {
            "snapshot_version": 1,
            "aid": self.state.aid,
            "role": self.agent.name,
            "model": getattr(self.agent, "model", None),
            "session_state": {
                "used_tokens": self.state.used_tokens,
                "context_tokens": self.state.context_tokens,
                "step_count": self.state.step_count,
                "markup_recovered": self.state.markup_recovered,
                "recent_call_hashes": list(self.state.recent_call_hashes),
                "reads_since_last_edit": self.state.reads_since_last_edit,
                "low_yield_since_progress": self.state.low_yield_since_progress,
                "distinct_evidence_count": self.state.distinct_evidence_count,
                "seen_result_hashes": sorted(self.state._seen_result_hashes),
                "scout_ledger": [dict(card) for card in self.state.scout_ledger],
                "steps_since_progress": self.state.steps_since_progress,
                "wind_down_done": self.state.wind_down_done,
                "wind_down_token_mark": self.state.wind_down_token_mark,
                "forced_unsatisfied": self.state.forced_unsatisfied,
                "loop_blocked_since_progress": self.state.loop_blocked_since_progress,
                "extension_offered": self.state.extension_offered,
                "extensions_granted": self.state.extensions_granted,
                "extension_reasons": list(self.state.extension_reasons),
                "phase": self.state.phase.value,
                "terminal_reason": self.state.terminal_reason,
                "pending_events": [_serialize_pending_row(row) for row in self.state.pending_events.rows.values()],
            },
        }
        if self.state.pending_user_messages:
            meta["pending_messages"] = self.state.enriched_pending_user_messages()
        return (
            copy.deepcopy(self.state.enriched_messages()),
            copy.deepcopy(meta),
        )

    def _prepare_auto_save(self) -> Callable[[], None] | None:
        if not self._auto_save_path:
            return None
        path = self._auto_save_path
        messages, meta = self._snapshot_for_save()
        return lambda: self.store.save(path, messages, meta=meta)

    def enqueue_auto_save(self) -> asyncio.Task[None] | None:
        if self._auto_save_subscriber is None:
            return None
        return self._auto_save_subscriber.enqueue()

    def _auto_save(self) -> None:
        if self._auto_save_path:
            self.save(self._auto_save_path)


def _snapshot_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _snapshot_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _serialize_pending_row(row: PendingRow) -> dict[str, object]:
    return {
        "tool_call_id": row.tool_call_id,
        "kind": row.kind.value,
        "order": row.order,
        "ref": row.ref,
        "status": row.status.value,
        "result": row.result,
        "error": row.error,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _restore_pending_row(value: object) -> PendingRow | None:
    if not isinstance(value, dict):
        return None
    try:
        tool_call_id = str(value["tool_call_id"])
        kind = RowKind(str(value["kind"]))
        status = RowStatus(str(value.get("status", RowStatus.PENDING.value)))
        order = int(value["order"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    result = value.get("result")
    error = value.get("error")
    finished_at = value.get("finished_at")
    if status is RowStatus.PENDING:
        status = RowStatus.FAILED
        error = "deferred child interrupted by session restore"
        result = error
        finished_at = None
    return PendingRow(
        tool_call_id=tool_call_id,
        kind=kind,
        order=order,
        ref=value.get("ref"),
        status=status,
        result=str(result) if result is not None else None,
        error=str(error) if error is not None else None,
        started_at=value.get("started_at"),
        finished_at=finished_at,
    )


__all__ = [
    "Session",
    "SessionRuntime",
]
