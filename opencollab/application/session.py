from __future__ import annotations

import asyncio
import copy
import inspect
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import (
    EnvironmentPort,
    JournalSnapshotStorePort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SessionStorePort,
    SnapshotStorePort,
    TracePort,
)
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.session_snapshot import (
    _restore_active_turn_start,
    _restore_pending_row,
    _restore_phase,
    _restore_queued_external_user_turn,
    _serialize_pending_row,
    _snapshot_int,
    _snapshot_nonnegative_float,
    _snapshot_nonnegative_int,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tool_loops import LoopState

if TYPE_CHECKING:
    from opencollab.application.scheduler import LaunchSpec


class SessionBusyError(RuntimeError):
    """Raised when a public operation would interleave an active session turn."""


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
    owns_llm: bool = False
    run_id: str | None = None


class Session:
    """Public facade for a stateful agent session.

    This application-layer facade owns session lifecycle state access but not
    concrete collaborator construction. Callers must pass a pre-built
    ``SessionRuntime`` from the composition root.
    A missing tracer inherits the runtime's recorder. Assigning ``None`` to
    ``session.tracer`` after construction disables recording.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        runtime: SessionRuntime,
        env: EnvironmentPort | None = None,
        tracer: TracePort | None = None,
        max_budget_tokens: int | None = 1_000_000,
        max_steps: int | None = 100,
        auto_save_path: str | None = None,
        permission_policy: PermissionPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
    ):
        self.agent = runtime.runner.agent
        self.env = env
        self.tracer = tracer if tracer is not None else runtime.runner.tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self._permission_policy = permission_policy
        self._safety_policy = safety_policy
        self._auto_save_path = auto_save_path
        self.run_id = runtime.run_id
        self._launch_state = "not_applied"
        self._applied_launch: object | None = None
        # The public facade owns turn admission. The runner protects its own
        # cleanup, but callers can otherwise interleave a new message or a
        # second run with the same mutable FSM.
        self._turn_lock = asyncio.Lock()

        # Adopt the runtime's collaborators as Session attributes so the
        # public surface stays exactly what it used to be.
        self.state = runtime.state
        self.event_bus = runtime.event_bus
        self._llm = runtime.llm
        self.store = runtime.store
        self.tool_execution = runtime.tool_execution
        self.tool_execution.loop_reservation_checkpoint = self._checkpoint_loop_reservation
        self.runner = runtime.runner
        # Set again now that the runner and the tool executor exist, so both
        # write through this session's identity rather than the one the
        # runtime was built with.
        self.tracer = self._tracer
        self._auto_save_subscriber = runtime.auto_save_subscriber
        self.runner.late_provider_usage_checkpoint = self.enqueue_auto_save
        self._owns_llm = runtime.owns_llm
        self._llm_closed = False
        self._llm_close_error: BaseException | None = None
        self._terminal_checkpoint_error: Exception | None = None
        self._auto_save_sequence = 0
        self._auto_save_reserved_sequence = 0
        self._auto_save_message_count = 0
        self._auto_save_rewrite_from: int | None = 0
        self._auto_save_rewrite_revision = 0
        self._auto_save_lock = threading.Lock()
        self._auto_save_checkpoint_lock = threading.RLock()
        self._auto_save_pending_checkpoints: set[int] = set()
        self._auto_save_seen_result_hashes: set[str] = set()
        self._next_auto_save_checkpoint = 1
        self._loop_checkpoint_results: list[dict] = []
        self._loop_checkpoint_prefix: list[dict] = []
        self._loop_checkpoint_step = -1
        # The env attribute mirrors the runtime's tool_execution env so
        # downstream readers (snapshot, characterization tests) still see
        # the same Environment instance.
        if env is None:
            self.env = self.tool_execution.environment

    @property
    def env(self) -> EnvironmentPort | None:
        return self._env

    @env.setter
    def env(self, value: EnvironmentPort | None) -> None:
        self._env = value
        if hasattr(self, "tool_execution"):
            self.tool_execution.environment = value

    @property
    def tracer(self) -> TracePort | None:
        return self._tracer

    @tracer.setter
    def tracer(self, value: TracePort | None) -> None:
        # Bind any tracer that supports it to this session, read live: a
        # snapshot restores ``state.aid`` after the tracer is set, and a
        # caller may hand in a plain tracer at any point.
        for_identity = getattr(value, "for_identity", None)
        if callable(for_identity):
            value = for_identity(self._trace_identity)
        self._tracer = value
        if hasattr(self, "tool_execution"):
            self.tool_execution.tracer = value
        if hasattr(self, "runner"):
            self.runner.tracer = value

    def _trace_identity(self) -> tuple[int | None, Any]:
        state = getattr(self, "state", None)
        return (
            getattr(state, "aid", None) if state is not None else None,
            getattr(self.agent, "name", None),
        )

    @property
    def max_budget_tokens(self) -> int | None:
        return self.runner.max_budget_tokens if hasattr(self, "runner") else self._max_budget_tokens

    @max_budget_tokens.setter
    def max_budget_tokens(self, value: int | None) -> None:
        self._max_budget_tokens = value
        if hasattr(self, "runner"):
            self.runner.max_budget_tokens = value

    @property
    def max_steps(self) -> int | None:
        return self._max_steps

    @max_steps.setter
    def max_steps(self, value: int | None) -> None:
        self._max_steps = value
        if hasattr(self, "runner"):
            self.runner.max_steps = value

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
    def pending_execution_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Model and tool operations still settling after the turn returned."""
        owned = {
            *self.runner.pending_cleanup_tasks,
            *getattr(self.tool_execution, "pending_cleanup_tasks", ()),
        }
        return tuple(task for task in owned if not task.done())

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Execution and subscriber work that must finish before teardown."""
        owned = {*self.pending_execution_tasks, *self.event_bus.pending_tasks}
        return tuple(task for task in owned if not task.done())

    @property
    def persistence_errors(self) -> tuple[Exception, ...]:
        """Sticky subscriber persistence failures visible to run boundaries."""
        errors: list[Exception] = []
        seen: set[int] = set()
        for subscriber in self.event_bus.subscribers:
            error = getattr(subscriber, "last_error", None)
            if isinstance(error, Exception) and id(error) not in seen:
                errors.append(error)
                seen.add(id(error))
        error = self._terminal_checkpoint_error
        if isinstance(error, Exception) and id(error) not in seen:
            errors.append(error)
        return tuple(errors)

    async def aclose(self) -> None:
        """Close the model transport when this session created and owns it."""
        if self._llm_close_error is not None:
            raise self._llm_close_error
        if not self._owns_llm or self._llm_closed:
            self.runner.finish_run_control()
            return
        self._llm_closed = True
        close = getattr(self._llm, "close", None)
        if not callable(close):
            close = getattr(self._llm, "aclose", None)
        if callable(close):
            try:
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome
            except BaseException as exc:
                self._llm_close_error = exc
                self.runner._emit_run_event("error", error_type=type(exc).__name__, message=str(exc))
                raise
        self.runner.finish_run_control()

    @property
    def messages(self) -> list[dict]:
        return self.state.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.state.replace_messages(value)
        if hasattr(self, "_auto_save_rewrite_from"):
            with self._auto_save_lock:
                self._auto_save_rewrite_from = 0
                self._auto_save_rewrite_revision += 1

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
        return self.state.turn.recent_call_hashes

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
        if self._turn_lock.locked() or self.runner.pending_cleanup_tasks:
            raise SessionBusyError("session already has an active turn")
        async with self._turn_lock:
            primary_error: BaseException | None = None
            try:
                return await self.runner.run_loop(cancel_event)
            except asyncio.CancelledError as exc:
                # A public caller may cancel the task directly instead of
                # setting ``cancel_event``.  The runner intentionally lets that
                # cancellation propagate, but leaving an in-flight phase in
                # place would make the facade permanently reject the next user
                # turn as busy.  Record the same terminal state used by the
                # scheduler's out-of-band cancellation path before re-raising.
                primary_error = exc
                if not self.state.phase.is_terminal():
                    self.state.cancel()
                self._append_restore_results_for_open_tool_calls(
                    content=(
                        "Tool execution interrupted by session cancellation. "
                        "The execution outcome is unknown."
                    ),
                    exclude_ids=set(self.state.pending_events.rows),
                )
                raise
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                self.runner._record_run_terminal()
                try:
                    await self._checkpoint_terminal_snapshot()
                except Exception as checkpoint_error:
                    if primary_error is None:
                        raise
                    add_exception_note(
                        primary_error,
                        "terminal checkpoint also failed: "
                        f"{type(checkpoint_error).__name__}: {checkpoint_error}",
                    )

    async def _checkpoint_terminal_snapshot(self) -> None:
        if not self.state.phase.is_terminal() and self.state.phase is not SessionPhase.AWAITING_EVENTS:
            return
        owner = self.enqueue_auto_save()
        if owner is not None:
            await await_owned_operation(
                owner,
                propagate_cancellation=True,
            )
        if self.state.phase is SessionPhase.AWAITING_EVENTS:
            # The pending rows and their elapsed time are now complete. Persist
            # this suspension without emitting the step_end reserved for resume.
            return
        # Journal-backed autosave normally appends a delta. A terminal turn
        # must also publish a self-contained base snapshot: callers and crash
        # recovery may read the base file directly, without replaying its
        # sidecar journal.
        if (
            not self._auto_save_path
            or not isinstance(self.store, JournalSnapshotStorePort)
        ):
            return

        async def checkpoint() -> None:
            try:
                await asyncio.to_thread(self.save, self._auto_save_path)
            except Exception as exc:
                self._terminal_checkpoint_error = exc
                raise

        await await_owned_operation(
            checkpoint(),
            propagate_cancellation=True,
        )

    async def add_user_message(self, content: str) -> None:
        if (
            self._turn_lock.locked()
            or self.runner.pending_cleanup_tasks
            or self.state.pending_external_user_turn is not None
            or (self.state.phase is not SessionPhase.IDLE and not self.state.phase.is_terminal())
        ):
            raise SessionBusyError("session already has an active turn")
        async with self._turn_lock:
            self.state.append_queued_external_user_turn(
                content, defer=not self.state.pending_events.is_empty()
            )
            self.state.reset_for_user_turn()
            self.runner.reset_runtime_for_user_turn()
            await self.event_bus.emit(SessionEvent(type="user_message_appended"))

    def apply_launch(self, launch: "LaunchSpec") -> None:
        """Apply launch-time persistence as a one-shot lifecycle step.

        Resumes from ``launch.session_file`` if it exists, otherwise seeds the
        auto-save file. Idempotent: a second call is a no-op, so live message
        state is never clobbered by a re-resume and the seed file is not
        re-truncated. The ongoing per-event ``AutoSaveSubscriber`` (wired at
        construction) is a separate concern and unaffected.
        """
        if self._launch_state == "applied":
            if launch != self._applied_launch:
                raise ValueError(
                    "session already applied a different launch specification"
                )
            return
        if self._launch_state == "applying":
            raise SessionBusyError("session launch persistence is already applying")

        self._launch_state = "applying"
        try:
            resumable = bool(
                launch.session_file
                and (
                    os.path.exists(launch.session_file)
                    or (
                        isinstance(self.store, JournalSnapshotStorePort)
                        and self.store.has_snapshot(launch.session_file)
                    )
                )
            )
            if launch.session_file and resumable:
                self.restore(launch.session_file)
            elif launch.auto_save_path:
                self.save(launch.auto_save_path)
        except BaseException:
            self._launch_state = "not_applied"
            raise
        else:
            self._applied_launch = launch
            self._launch_state = "applied"

    def restore(self, path: str) -> None:
        """Restore a complete snapshot while accepting legacy message-only stores."""
        if self._turn_lock.locked() or self.pending_cleanup_tasks:
            raise SessionBusyError("session cannot be restored while runtime work is active")
        if isinstance(self.store, SnapshotStorePort):
            snapshot = self.store.load_snapshot(path, self.agent.system_prompt)
        else:
            snapshot = {
                "messages": self.store.load_messages(path, self.agent.system_prompt)
            }

        messages = list(snapshot.get("messages", []))
        raw_state = snapshot.get("session_state")
        restored = SessionState(
            messages=[],
            aid=_snapshot_int(snapshot.get("aid"), default=self.state.aid),
        )
        restored.replace_messages(messages)
        pending_messages = snapshot.get("pending_messages", [])
        restored.pending_user_messages = (
            [
                dict(message)
                for message in pending_messages
                if isinstance(message, dict)
            ]
            if isinstance(pending_messages, list)
            else []
        )
        if not isinstance(raw_state, dict):
            # A message-only (or pre-runtime-state) snapshot has no authority
            # over the current session's counters, phase, or pending work. Build
            # a complete clean state first, then update the existing object so
            # the runner and tool executor retain their shared state reference.
            self._append_restore_results_for_open_tool_calls(restored)
            self._publish_restored_state(restored)
            self._restore_auto_save_tracking(snapshot)
            self._checkpoint_restored_auto_save_target(path)
            return

        restored.pending_external_user_turn = _restore_queued_external_user_turn(
            raw_state.get("pending_external_user_turn"), restored.messages
        )
        restored_turn_start = _restore_active_turn_start(
            raw_state.get("active_turn_start_message_index"), restored.messages
        )

        restored.set_used_tokens(_snapshot_nonnegative_int(raw_state.get("used_tokens")))
        restored.set_context_tokens(
            _snapshot_nonnegative_int(raw_state.get("context_tokens"))
        )
        restored.set_step_count(_snapshot_nonnegative_int(raw_state.get("step_count")))
        restored.markup_recovered = _snapshot_nonnegative_int(
            raw_state.get("markup_recovered")
        )
        hashes = raw_state.get("recent_call_hashes")
        restored.replace_recent_tool_hashes(
            [str(value) for value in hashes] if isinstance(hashes, list) else []
        )
        restored.turn.reads_since_last_edit = _snapshot_nonnegative_int(
            raw_state.get("reads_since_last_edit")
        )
        restored.turn.has_landed_write = bool(
            raw_state.get("has_landed_write", False)
        )
        restored.turn.low_yield_since_progress = _snapshot_nonnegative_int(
            raw_state.get("low_yield_since_progress")
        )
        restored.turn.distinct_evidence_count = _snapshot_nonnegative_int(
            raw_state.get("distinct_evidence_count")
        )
        seen_hashes = raw_state.get("seen_result_hashes")
        restored.turn.seen_result_hashes = (
            {str(value) for value in seen_hashes}
            if isinstance(seen_hashes, list)
            else set()
        )
        ledger = raw_state.get("scout_ledger")
        restored.turn.scout_ledger = (
            [dict(card) for card in ledger if isinstance(card, dict)]
            if isinstance(ledger, list)
            else []
        )
        restored.turn.steps_since_progress = _snapshot_nonnegative_int(
            raw_state.get("steps_since_progress")
        )
        restored.wind_down_done = bool(raw_state.get("wind_down_done", False))
        if "wind_down_attempts" in raw_state:
            restored.wind_down_attempts = _snapshot_nonnegative_int(
                raw_state.get("wind_down_attempts")
            )
        else:
            # A legacy snapshot cannot prove whether its one retry was already
            # allocated. Preserve the budget contract by not granting another.
            restored.wind_down_attempts = 2 if restored.wind_down_done else 0
        restored.wind_down_token_mark = _snapshot_nonnegative_int(
            raw_state.get("wind_down_token_mark")
        )
        restored.budget_reserve_consumed = bool(
            raw_state.get("budget_reserve_consumed", restored.wind_down_done)
        )
        restored.turn.loop_blocked_since_progress = _snapshot_nonnegative_int(
            raw_state.get("loop_blocked_since_progress")
        )
        restored.turn.loop_state = LoopState.from_dict(
            raw_state.get("loop_state"),
            legacy_blocked_calls=restored.turn.loop_blocked_since_progress,
            active_keys=restored.turn.recent_call_hashes,
        )
        restored.turn.write_effect_unknown = raw_state.get("write_effect_unknown") is True
        restored.turn.last_progress_unknown = raw_state.get("last_progress_unknown") is True
        rows = raw_state.get("pending_events", [])
        if isinstance(rows, list):
            for value in rows:
                row = _restore_pending_row(value)
                if row is not None:
                    restored.pending_events.add(row)

        phase = _restore_phase(raw_state.get("phase", SessionPhase.IDLE.value))
        if phase is SessionPhase.AWAITING_EVENTS:
            self._complete_missing_pending_rows(restored)
            if restored_turn_start is None:
                # Legacy and malformed snapshots may not carry a valid answer
                # cursor. Future messages begin at the restored transcript end,
                # which excludes prior answers while retaining the resumed one.
                restored_turn_start = len(restored.messages)
        # In-flight provider/tool phases depend on process-local coroutines that
        # cannot survive restart. AWAITING_EVENTS is recoverable because pending
        # child rows above are converted to explicit FAILED tool results.
        recoverable = {SessionPhase.AWAITING_EVENTS, *[p for p in SessionPhase if p.is_terminal()]}
        restored.set_phase(phase if phase in recoverable else SessionPhase.IDLE)
        if restored.phase is SessionPhase.AWAITING_EVENTS:
            restored.active_turn_start_message_index = restored_turn_start
        if phase is not SessionPhase.AWAITING_EVENTS:
            if not restored.pending_events.is_empty():
                self._complete_missing_pending_rows(restored)
            open_ids = set(self._open_tool_call_ids(restored))
            for message in restored.pending_events.ordered_results():
                if message["tool_call_id"] in open_ids:
                    restored.append_message(message)
            self._append_restore_results_for_open_tool_calls(
                restored, completed_results=raw_state.get("loop_checkpoint_results"),
            )
            # Rows from an interrupted non-awaiting phase have no live producer
            # after process restart. The explicit tool results above close the
            # provider protocol; keeping the stale sidecar rows would make the
            # scheduler's quiescence check wait forever after the resumed turn.
            restored.pending_events.clear()
        if (
            restored.phase is SessionPhase.AWAITING_EVENTS
            and restored.pending_events.is_empty()
        ):
            restored.set_phase(SessionPhase.IDLE)
            self._append_restore_results_for_open_tool_calls(restored)
        restored.pending_step_latency = (
            _snapshot_nonnegative_float(raw_state.get("pending_step_latency"))
            if restored.phase is SessionPhase.AWAITING_EVENTS
            else None
        )
        submitted_summary = raw_state.get("submitted_summary")
        restored.submitted_summary = (
            submitted_summary
            if restored.phase is SessionPhase.AWAITING_EVENTS and isinstance(submitted_summary, str)
            else None
        )
        restored.terminal_reason = (
            str(raw_state["terminal_reason"])
            if restored.phase.is_terminal()
            and raw_state.get("terminal_reason") is not None
            else None
        )
        self._publish_restored_state(restored)
        self._restore_auto_save_tracking(snapshot)
        self._checkpoint_restored_auto_save_target(path)

    def _restore_auto_save_tracking(self, snapshot: dict[str, Any]) -> None:
        sequence = _snapshot_nonnegative_int(snapshot.get("_autosave_sequence"))
        self._auto_save_sequence = sequence
        self._auto_save_reserved_sequence = sequence
        stored_messages = snapshot.get("messages")
        self._auto_save_message_count = min(
            len(stored_messages) if isinstance(stored_messages, list) else 0,
            len(self.state.messages),
        )
        self._auto_save_rewrite_from = None
        self._auto_save_seen_result_hashes = set(
            self.state.turn.seen_result_hashes
        )
        self._next_auto_save_checkpoint = (
            1 if sequence == 0 else 1 << sequence.bit_length()
        )

    def _checkpoint_restored_auto_save_target(self, source_path: str) -> None:
        target_path = self._auto_save_path
        if target_path and not _same_snapshot_path(source_path, target_path):
            self.save(target_path)

    def _publish_restored_state(self, restored: SessionState) -> None:
        """Publish one fully validated restore while retaining shared identity."""
        self.runner.reset_for_restore()
        self.state.__dict__.clear()
        self.state.__dict__.update(restored.__dict__)
        self._loop_checkpoint_results = []
        self._loop_checkpoint_prefix = []
        self._loop_checkpoint_step = -1

    def _open_tool_call_ids(
        self,
        state: SessionState | None = None,
    ) -> list[str]:
        # Pair calls and results in transcript order. Tool-call ids are expected
        # to be unique, but several OpenAI-compatible providers reuse short ids
        # such as ``call_1`` on later turns. A global "answered ids" set would
        # let an old result incorrectly close a newer interrupted call.
        open_ids: list[str] = []
        restored_state = state or self.state
        for message in restored_state.messages:
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

    def _complete_missing_pending_rows(
        self,
        state: SessionState | None = None,
    ) -> None:
        restored_state = state or self.state
        existing = set(restored_state.pending_events.rows)
        for order, tool_call_id in enumerate(
            self._open_tool_call_ids(restored_state)
        ):
            if tool_call_id in existing:
                continue
            error = "deferred child state missing during session restore"
            restored_state.pending_events.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.CHILD_AGENT,
                    order=order,
                    status=RowStatus.FAILED,
                    result=error,
                    error=error,
                )
            )

    def _append_restore_results_for_open_tool_calls(
        self,
        state: SessionState | None = None,
        *,
        content: str = "Tool execution interrupted by session restore.",
        exclude_ids: set[str] | None = None,
        completed_results: object = None,
    ) -> None:
        """Close assistant tool calls whose process-local execution was lost."""
        restored_state = state or self.state
        receipts = {
            item["tool_call_id"]: item["content"]
            for item in completed_results if isinstance(item, dict)
            and isinstance(item.get("tool_call_id"), str) and isinstance(item.get("content"), str)
        } if isinstance(completed_results, list) else {}
        for order, tool_call_id in enumerate(self._open_tool_call_ids(restored_state)):
            # Cancellation must retain rows whose delegated work still has a
            # producer. Restore has no surviving producer and closes all calls.
            if exclude_ids is not None and tool_call_id in exclude_ids:
                continue
            if exclude_ids is not None and not restored_state.pending_events.is_empty():
                restored_state.pending_events.add(PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.IMMEDIATE,
                    order=order,
                    status=RowStatus.FAILED,
                    result=content,
                    error=content,
                ))
                continue
            restored_state.append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": receipts.get(tool_call_id, content),
                }
            )

    def save(self, path: str) -> None:
        if (
            _same_snapshot_path(path, self._auto_save_path)
            and isinstance(self.store, JournalSnapshotStorePort)
        ):
            # Freeze and publish after every operation already submitted by
            # this session. An older append may finish later, but replay will
            # skip its sequence and its completion cannot rewind our cursor.
            with self._auto_save_lock:
                messages, meta = self._snapshot_for_save()
                sequence = self._reserve_auto_save_sequence()
                rewrite_revision = self._auto_save_rewrite_revision
                self._auto_save_pending_checkpoints.add(sequence)
            try:
                with self._auto_save_checkpoint_lock:
                    with self._auto_save_lock:
                        if sequence < self._auto_save_sequence:
                            return
                    self.store.checkpoint_snapshot(path, messages, meta=meta, sequence=sequence)
                    with self._auto_save_lock:
                        self._auto_save_sequence = sequence
                        self._auto_save_message_count = len(messages)
                        if self._auto_save_rewrite_revision == rewrite_revision:
                            self._auto_save_rewrite_from = None
                        self._auto_save_seen_result_hashes = set(meta["session_state"]["seen_result_hashes"])
                        self._next_auto_save_checkpoint = 1 << sequence.bit_length()
            finally:
                with self._auto_save_lock:
                    self._auto_save_pending_checkpoints.discard(sequence)
            return
        messages, meta = self._snapshot_for_save()
        self.store.save(path, messages, meta=meta)

    def _snapshot_for_save(self, message_start: int = 0) -> tuple[list[dict], dict]:
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
                "recent_call_hashes": list(self.state.turn.recent_call_hashes),
                "reads_since_last_edit": self.state.turn.reads_since_last_edit,
                "has_landed_write": self.state.turn.has_landed_write,
                "low_yield_since_progress": self.state.turn.low_yield_since_progress,
                "distinct_evidence_count": self.state.turn.distinct_evidence_count,
                "seen_result_hashes": sorted(self.state.turn.seen_result_hashes),
                "scout_ledger": [dict(card) for card in self.state.turn.scout_ledger],
                "steps_since_progress": self.state.turn.steps_since_progress,
                "wind_down_done": self.state.wind_down_done,
                "wind_down_attempts": self.state.wind_down_attempts,
                "wind_down_token_mark": self.state.wind_down_token_mark,
                "budget_reserve_consumed": self.state.budget_reserve_consumed,
                "loop_blocked_since_progress": self.state.turn.loop_blocked_since_progress,
                "loop_state": self.state.turn.loop_state.to_dict(),
                "write_effect_unknown": self.state.turn.write_effect_unknown,
                "last_progress_unknown": self.state.turn.last_progress_unknown,
                "phase": self.state.phase.value,
                "terminal_reason": self.state.terminal_reason,
                "pending_events": [_serialize_pending_row(row) for row in self.state.pending_events.rows.values()],
                "pending_external_user_turn": copy.deepcopy(
                    self.state.pending_external_user_turn
                ),
                "active_turn_start_message_index": self.state.active_turn_start_message_index,
                "pending_step_latency": self.state.pending_step_latency,
                "submitted_summary": self.state.submitted_summary,
            },
        }
        if self.run_id is not None:
            meta["run_id"] = self.run_id
        if self.state.step_count != self._loop_checkpoint_step or self.state.phase in {
            SessionPhase.AUTOSAVING, SessionPhase.AWAITING_EVENTS, SessionPhase.DONE,
        }:
            self._loop_checkpoint_results = []
            self._loop_checkpoint_prefix = []
        if self._loop_checkpoint_results or self._loop_checkpoint_prefix:
            meta["session_state"]["loop_checkpoint_results"] = [
                dict(row) for row in (*self._loop_checkpoint_prefix, *self._loop_checkpoint_results)
            ]
        if self.state.pending_user_messages:
            meta["pending_messages"] = self.state.enriched_pending_user_messages()
        return (
            copy.deepcopy(self.state.enriched_messages(message_start)),
            copy.deepcopy(meta),
        )

    def _prepare_auto_save(self) -> Callable[[], None] | None:
        if not self._auto_save_path:
            return None
        path = self._auto_save_path
        if not isinstance(self.store, JournalSnapshotStorePort):
            messages, meta = self._snapshot_for_save()
            return lambda: self.store.save(path, messages, meta=meta)

        with self._auto_save_lock:
            return self._prepare_journal_auto_save(path)

    def _reserve_auto_save_sequence(self) -> int:
        self._auto_save_reserved_sequence = max(
            self._auto_save_reserved_sequence, self._auto_save_sequence,
        ) + 1
        return self._auto_save_reserved_sequence

    def _prepare_journal_auto_save(self, path: str) -> Callable[[], None]:
        preceding_write_pending = self._auto_save_reserved_sequence > self._auto_save_sequence
        sequence = self._reserve_auto_save_sequence()
        message_count = len(self.state.messages)
        # A pending full checkpoint may shorten the base before this delta
        # reaches storage. Freeze its complete replacement on the event loop.
        replace_from = 0 if preceding_write_pending else min(self._auto_save_message_count, message_count)
        if replace_from:
            # The run loop can fold steering into the most recent persisted
            # user message, so retain one-message overlap without re-copying
            # the complete transcript.
            replace_from -= 1
        rewrite_revision = self._auto_save_rewrite_revision
        if self._auto_save_rewrite_from is not None:
            replace_from = min(replace_from, self._auto_save_rewrite_from)
        messages, meta = self._snapshot_for_save(replace_from)
        current_seen_hashes = set(self.state.turn.seen_result_hashes)
        seen_hashes_reset = preceding_write_pending or not self._auto_save_seen_result_hashes.issubset(
            current_seen_hashes
        )
        seen_hashes_added = sorted(
            current_seen_hashes
            if seen_hashes_reset
            else current_seen_hashes - self._auto_save_seen_result_hashes
        )
        checkpoint_due = sequence >= self._next_auto_save_checkpoint
        checkpoint_messages = (
            copy.deepcopy(self.state.enriched_messages())
            if checkpoint_due
            else None
        )

        def persist() -> None:
            self.store.append_snapshot_delta(
                path,
                sequence=sequence,
                replace_from=replace_from,
                messages=messages,
                meta=meta,
                seen_result_hashes_reset=seen_hashes_reset,
                seen_result_hashes_added=seen_hashes_added,
            )
            with self._auto_save_lock:
                if sequence <= self._auto_save_sequence:
                    return
                # The journal append is already fsync'd. Advance the absolute
                # cursor before optional compaction so a failed base rewrite
                # cannot cause the durable delta to be skipped on the next save.
                self._auto_save_sequence = sequence
                self._auto_save_message_count = message_count
                if self._auto_save_rewrite_revision == rewrite_revision:
                    self._auto_save_rewrite_from = None
                self._auto_save_seen_result_hashes = current_seen_hashes
            if checkpoint_messages is not None:
                with self._auto_save_checkpoint_lock:
                    with self._auto_save_lock:
                        if sequence < self._auto_save_sequence:
                            return
                    self.store.checkpoint_snapshot(
                        path,
                        checkpoint_messages,
                        meta=meta,
                        sequence=sequence,
                    )
                    with self._auto_save_lock:
                        self._next_auto_save_checkpoint = 1 << sequence.bit_length()

        def persist_incrementally() -> None:
            with self._auto_save_lock:
                preceding_checkpoint_pending = any(
                    pending < sequence for pending in self._auto_save_pending_checkpoints
                )
            if preceding_checkpoint_pending:
                # Only a persistence worker waits for disk I/O. An older
                # already-running append remains free to finish behind the
                # newer manual checkpoint and will be skipped on replay.
                with self._auto_save_checkpoint_lock:
                    persist()
            else:
                persist()

        return persist_incrementally

    def enqueue_auto_save(self) -> asyncio.Task[None] | None:
        if self._auto_save_subscriber is None:
            return None
        return self._auto_save_subscriber.enqueue()

    async def _checkpoint_loop_reservation(self, completed_results: list[dict]) -> None:
        """Persist a consumed extra dispatch through the existing save owner."""
        if self._auto_save_path is None:
            return
        pending = [
            {"role": "tool", "tool_call_id": row.tool_call_id, "content": row.result or ""}
            for row in self.state.pending_events.rows.values() if row.status is not RowStatus.PENDING
        ]
        # Retain the live prefix until this model batch finishes. A cancellation
        # or failed tool can trigger a later terminal save of the same step.
        self._loop_checkpoint_prefix = pending
        self._loop_checkpoint_results = completed_results
        self._loop_checkpoint_step = self.state.step_count
        subscriber = self._auto_save_subscriber
        failures = subscriber.failure_count if subscriber is not None else 0
        owner = self.enqueue_auto_save()
        if owner is not None:
            await await_owned_operation(owner, propagate_cancellation=True)
            if subscriber.failure_count != failures:
                raise RuntimeError("loop retry reservation could not be persisted") from subscriber.last_error
        else:
            await await_owned_operation(
                asyncio.to_thread(self.save, self._auto_save_path), propagate_cancellation=True,
            )

    def _auto_save(self) -> None:
        if self._auto_save_path:
            self.save(self._auto_save_path)


def _same_snapshot_path(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


__all__ = [
    "Session",
    "SessionRuntime",
]
