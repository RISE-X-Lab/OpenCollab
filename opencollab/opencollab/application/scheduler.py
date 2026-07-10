"""Scheduler — passive process tracker for multi-agent execution.

Tracks every Session as a SessionControlBlock in a SessionTable:
- spawn() is non-blocking (returns aid immediately)
- Agents run in parallel via asyncio.create_task
- Results are delivered to parents via message injection into parent state

The class is assembled from three cohesive mixins, each in its own module:
- ``LifecycleMixin`` (``scheduler_lifecycle``) — register/spawn/drive/wake/deliver
- ``MessagingMixin`` (``scheduler_messaging``) — teammate-message inbox
- ``InflightDedupMixin`` (``scheduler_dedup``) — single-flight spawn dedup

This module keeps the run-loop orchestration, roster snapshots, and the shared
helpers (manifest/autosave, topology check, event emission) the mixins rely on.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import logging
import math
from typing import Any, Callable

from opencollab.application.async_timeout import force_task_terminal
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.events import (
    SchedulerEventFactory,
    default_scheduler_event_factory,
)
from opencollab.application.ports import (
    DiffCapablePort,
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SessionFactoryPort,
    TracePort,
    WorktreePoolPort,
)
from opencollab.application.scheduler_dedup import InflightDedupMixin
from opencollab.application.scheduler_lifecycle import LifecycleMixin
from opencollab.application.scheduler_messaging import MessagingMixin
from opencollab.application.scheduler_types import LaunchSpec, QueuedTeammateMessage
from opencollab.application.self_collaboration import run_spawn_with_review
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.identity import role_collision_key, validate_role_identity
from opencollab.domain.pending import PendingRowError, RowStatus
from opencollab.domain.scheduler import SessionTable, lead_reserve, split_budget
from opencollab.domain.session import SessionPhase
from opencollab.domain.team import Topology

logger = logging.getLogger(__name__)

# Worktree diffs appended to a child's result are bounded so one huge diff
# can't blow up the parent's context; oversize diffs keep head + tail.
WORKTREE_DIFF_MAX_CHARS = 12_000
WORKTREE_DIFF_KEEP_CHARS = 6_000  # kept from each end when truncating
DEFAULT_SCHEDULER_CLEANUP_TIMEOUT = 10.0
MAX_FORCED_CLEANUP_TIMEOUT = 2.0


class Scheduler(LifecycleMixin, MessagingMixin, InflightDedupMixin):
    """Passive scheduler that tracks SCBs and runs agents in parallel.

    Design:
    - SessionTable holds all SCBs (pure data, no I/O)
    - spawn() creates a new SCB and schedules it for execution
    - run() drives the main loop, waiting for all agents to complete
    - Results are injected into parent sessions as system messages
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactoryPort,
        worktree_pool: WorktreePoolPort,
        event_sink: EventPublisherPort,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 1_000_000,
        permission_policy: PermissionPort | None = None,
        topology: Topology | None = None,
        roles: tuple[str, ...] = (),
        event_factory: SchedulerEventFactory | None = None,
    ):
        self._session_factory = session_factory
        self._worktree_pool = worktree_pool
        self._event_sink = event_sink
        self._events = event_factory or default_scheduler_event_factory()
        self._tracer = tracer
        self._max_budget_tokens = max_budget_tokens
        self._permission_policy = permission_policy
        self._topology = topology
        # Configured role names (from the team config), in declaration order.
        # Used by ``team_roster`` to surface the team before anything spawns.
        normalized_roles: list[str] = []
        role_keys: set[str] = set()
        for raw_role in roles:
            role = validate_role_identity(raw_role)
            collision_key = role_collision_key(role)
            if collision_key in role_keys:
                raise ValueError(
                    "configured scheduler roles collide after normalization"
                )
            role_keys.add(collision_key)
            normalized_roles.append(role)
        self._roles = tuple(normalized_roles)

        self.table = SessionTable()
        self._tasks: dict[int, asyncio.Task] = {}
        # ``spawn`` owns resources before the child driver exists. Track that
        # pre-driver window separately so cleanup can cancel/abort/finalize an
        # external spawn blocked in worktree setup or lifecycle event delivery.
        self._startup_tasks: dict[int, asyncio.Task[Any]] = {}
        self._startup_envs: dict[int, Any] = {}
        self._startup_origin: dict[int, tuple[int, str]] = {}
        self._sessions: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._run_lock = asyncio.Lock()
        self._active_run_tasks: set[asyncio.Task[Any]] = set()
        self._lead_turn_record: dict[str, Any] | None = None
        self._lead_session: Any | None = None
        # child aid -> (parent aid, tool_call_id) for deferred spawns: lets a
        # child's completion fill the exact pending row that suspended its
        # parent. Absent for legacy fire-and-forget spawns (tool_call_id=None).
        self._spawn_origin: dict[int, tuple[int, str]] = {}
        # Children whose result row was atomically filled before teardown began.
        # Cleanup may cancel their task while it is only emitting an observational
        # post-delivery event; this marker preserves the already-committed child /
        # parent terminal pair instead of rewriting only the child to CANCELLED.
        self._delivery_committed: set[int] = set()
        # Single-flight spawn dedup: task key -> the aid currently handling it,
        # plus the reverse map for release. A (role, task) is reserved at spawn
        # and freed when the child reaches a terminal phase, so a model that
        # re-issues an identical spawn is refused (see ``inflight_spawn``) rather
        # than spinning up a duplicate — tool-level enforcement of "don't spawn
        # the same task twice", which prompt guidance alone cannot guarantee.
        self._inflight: dict[str, int] = {}
        self._inflight_key_of: dict[int, str] = {}
        # Per-active-turn token leases. A lease records the session's token count
        # when the grant was made, so consumed tokens replace reserved headroom
        # instead of being counted twice. Releasing a terminal turn returns only
        # its *unspent* grant; tokens already consumed remain in ``used_tokens``.
        # The Lead lease is separate because several arithmetic tests seed it
        # without registering a real aid=0 session.
        self._lead_reservation: tuple[int, int] | None = None
        self._child_reservation: dict[int, int] = {}
        self._reservation_baseline: dict[int, int] = {}
        # aid -> queued teammate messages waiting to be appended as user
        # messages once that session is not running or suspended on pending work.
        self._message_inbox: dict[int, list[QueuedTeammateMessage]] = {}
        # A recipient turn is transactional across ``add_user_message``. The
        # outer delivery task and its rollback record stay owned until the add
        # commits, letting cleanup cancel a blocked external send and restore the
        # pre-delivery state even when the inner add coroutine ignores cancellation.
        self._message_delivery_tasks: dict[int, asyncio.Task[Any]] = {}
        self._message_delivery_records: dict[int, dict[str, Any]] = {}
        # Optional bootstrap-injected callback that persists a team.json manifest
        # from the current roster. Kept as a callback so the application layer
        # stays free of filesystem I/O.
        self._manifest_writer: Callable[[], None] | None = None
        self._manifest_subscriber: AutoSaveSubscriber | None = None
        self._shutting_down = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._fallback_autosavers: dict[int, AutoSaveSubscriber] = {}
        self._scheduler_persistence_errors: list[Exception] = []
        # A synchronous review loop temporarily yields its caller's turn lease
        # through ordinary ``spawn`` calls. Context-local accounting records only
        # leases actually released by this review invocation, so an external API
        # call that never suspended its parent cannot accidentally expand the
        # Lead reservation when the review returns.
        self._review_parent_lease_tracker: contextvars.ContextVar[
            tuple[int, dict[str, int]] | None
        ] = contextvars.ContextVar("review_parent_lease_tracker", default=None)

    def _track_review_parent_lease_release(self, parent_aid: int, delta: int) -> None:
        tracker = self._review_parent_lease_tracker.get()
        if tracker is None or tracker[0] != parent_aid:
            return
        tracker[1]["outstanding"] = max(
            0, tracker[1].get("outstanding", 0) + delta
        )

    def set_manifest_writer(
        self,
        fn: Callable[[], None],
        *,
        prepare_fn: Callable[[], Callable[[], None] | None] | None = None,
    ) -> None:
        """Inject the team-manifest persister (called on every roster change)."""
        self._manifest_writer = fn
        self._manifest_subscriber = AutoSaveSubscriber(
            fn,
            prepare_fn=prepare_fn,
        )

    def _write_manifest(self) -> Exception | None:
        if self._manifest_writer is None or self._manifest_subscriber is None:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._manifest_writer()
            except Exception as exc:
                self._scheduler_persistence_errors.append(exc)
                logger.warning("manifest write failed: %s", exc)
                return exc
            return None
        try:
            self._manifest_subscriber.enqueue()
        except Exception as exc:
            self._scheduler_persistence_errors.append(exc)
            logger.warning("manifest write enqueue failed: %s", exc)
            return exc
        return None

    def _autosave_session(self, aid: int) -> asyncio.Task[None] | None:
        session = self._sessions.get(aid)
        if session is None:
            return None
        save_path = getattr(session, "auto_save_path", None)
        if not save_path:
            return None
        enqueue = getattr(session, "enqueue_auto_save", None)
        try:
            if callable(enqueue):
                return enqueue()
            save = getattr(session, "save", None)
            if not callable(save):
                return None
            subscriber = self._fallback_autosavers.get(aid)
            if subscriber is None:
                subscriber = AutoSaveSubscriber(
                    lambda: save(save_path),
                )
                self._fallback_autosavers[aid] = subscriber
            return subscriber.enqueue()
        except Exception as exc:
            self._scheduler_persistence_errors.append(exc)
            logger.warning("session auto-save enqueue failed for aid %s: %s", aid, exc)
            return None

    def _autosave_all_sessions(self) -> tuple[asyncio.Task[None], ...]:
        owners: list[asyncio.Task[None]] = []
        for aid in list(self._sessions):
            owner = self._autosave_session(aid)
            if owner is not None:
                owners.append(owner)
        return tuple(owners)

    @property
    def events(self) -> SchedulerEventFactory:
        """The scheduler-event builders (the orchestration event vocabulary)."""
        return self._events

    @property
    def used_tokens(self) -> int:
        """Total tokens across all agents."""
        return self.table.total_used_tokens

    @property
    def allocated_tokens(self) -> int:
        """Tokens spent plus the unspent part of every active turn lease."""
        return self._budget_committed()

    @property
    def budget_exhausted(self) -> bool:
        """True once the team's *aggregate* spend has reached the global cap.

        Defense-in-depth companion to the per-session budget check: even though
        reserve-at-allocation keeps the sum of grants under the ceiling, a
        session may overshoot its own cap (a single LLM turn returns more tokens
        than budgeted). This catches the team total regardless of how the spend
        is distributed across sessions.
        """
        return self.used_tokens >= self._max_budget_tokens

    def _seed_lead_reservation(self) -> None:
        """Seed the running allocation with the Lead's reserve (idempotent).

        Called when agent 0 is registered. The Lead keeps the full pool as its
        own cap (it is the parent), but for the purpose of dividing the pool
        among children it reserves only ``lead_reserve(total)``. Each live child
        receives at most one equal share from the remaining pool.
        """
        used = self._session_used_tokens(0)
        self._lead_reservation = (lead_reserve(self._max_budget_tokens), used)

    def _session_used_tokens(self, aid: int) -> int:
        scb = self.table.get(aid)
        if scb is not None:
            return max(0, int(getattr(scb.state, "used_tokens", 0) or 0))
        session = self._sessions.get(aid)
        return max(0, int(getattr(session, "used_tokens", 0) or 0))

    def _lease_remaining(self, aid: int, grant: int, baseline: int) -> int:
        used = self._session_used_tokens(aid)
        return max(0, grant - max(0, used - baseline))

    def _budget_committed(self) -> int:
        committed = max(0, self.used_tokens)
        if self._lead_reservation is not None:
            grant, baseline = self._lead_reservation
            has_registered_lead = (
                self._lead_session is not None
                and self._sessions.get(0) is self._lead_session
            )
            lead_used = self._session_used_tokens(0) if has_registered_lead else baseline
            committed += max(0, grant - max(0, lead_used - baseline))
        for aid, grant in self._child_reservation.items():
            committed += self._lease_remaining(
                aid, grant, self._reservation_baseline.get(aid, 0)
            )
        return committed

    def _reserve_child_budget(self, aid: int) -> int:
        """Grant a child its cap from the unallocated remainder and book it.

        Synchronous (no await): a duplicate / batched spawn that runs before the
        first child's await already sees the updated ``_allocated_tokens``.
        Returns the granted cap.
        """
        grant = split_budget(self._max_budget_tokens, self._budget_committed())
        self._child_reservation[aid] = grant
        self._reservation_baseline[aid] = self._session_used_tokens(aid)
        return grant

    def _reserve_turn_budget(self, aid: int) -> int:
        """Lease every currently available token to a resuming session turn."""
        self._release_turn_budget(aid)
        grant = max(0, self._max_budget_tokens - self._budget_committed())
        session = self._sessions.get(aid)
        baseline = self._session_used_tokens(aid)
        if aid == 0 and self._lead_session is not None and session is self._lead_session:
            self._lead_reservation = (grant, baseline) if grant > 0 else None
        elif grant > 0:
            self._child_reservation[aid] = grant
            self._reservation_baseline[aid] = baseline
        self._set_session_budget_limit(aid, baseline + grant)
        return grant

    def _set_session_budget_limit(self, aid: int, limit: int) -> None:
        session = self._sessions.get(aid)
        if session is None:
            return
        if hasattr(session, "max_budget_tokens"):
            session.max_budget_tokens = limit
        runner = getattr(session, "runner", None)
        if runner is not None and hasattr(runner, "max_budget_tokens"):
            runner.max_budget_tokens = limit

    def _reserve_message_budget(self, aid: int) -> bool:
        """Acquire a fresh lease before a terminal teammate starts another turn."""
        session = self._sessions.get(aid)
        if session is None:
            return False
        if aid == 0 and self._lead_session is not None and session is self._lead_session:
            grant = self._reserve_turn_budget(aid)
        elif aid in self._child_reservation:
            return True
        else:
            grant = self._reserve_child_budget(aid)
            baseline = self._session_used_tokens(aid)
            self._set_session_budget_limit(aid, baseline + grant)
            if grant <= 0:
                self._release_child_budget(aid)
        # When spend already reached the ceiling, run one zero-budget precheck so
        # the queued turn reaches an explicit budget terminal instead of leaving
        # a durable inbox entry that can never become runnable.
        return grant > 0 or self.budget_exhausted

    def _release_turn_budget(self, aid: int) -> tuple[int, int] | None:
        if (
            aid == 0
            and self._lead_session is not None
            and self._sessions.get(0) is self._lead_session
        ):
            lease = self._lead_reservation
            self._lead_reservation = None
            return lease
        grant = self._child_reservation.pop(aid, None)
        baseline = self._reservation_baseline.pop(aid, None)
        if grant is None:
            return None
        return grant, baseline or 0

    def _current_turn_budget(self, aid: int) -> tuple[int, int] | None:
        """Snapshot ``aid``'s lease without mutating the shared allocation."""
        if (
            aid == 0
            and self._lead_session is not None
            and self._sessions.get(0) is self._lead_session
        ):
            return self._lead_reservation
        grant = self._child_reservation.get(aid)
        if grant is None:
            return None
        return grant, self._reservation_baseline.get(aid, 0)

    def _restore_turn_budget(self, aid: int, lease: tuple[int, int] | None) -> None:
        if lease is None:
            return
        old_grant, old_baseline = lease
        session = self._sessions.get(aid)
        used = self._session_used_tokens(aid)
        old_remaining = max(0, old_grant - max(0, used - old_baseline))
        available = max(0, self._max_budget_tokens - self._budget_committed())
        grant = min(old_remaining, available)
        baseline = used
        if aid == 0 and self._lead_session is not None and session is self._lead_session:
            self._lead_reservation = (grant, baseline) if grant > 0 else None
        elif grant > 0:
            self._child_reservation[aid] = grant
            self._reservation_baseline[aid] = baseline
        self._set_session_budget_limit(aid, baseline + grant)

    def _release_child_budget(self, aid: int) -> None:
        """Reclaim a terminal child's reservation so later spawns can reuse it.

        Idempotent: a child is finalized at most once per reservation.
        """
        grant = self._child_reservation.pop(aid, None)
        if grant is not None:
            self._reservation_baseline.pop(aid, None)

    @property
    def lead_session(self) -> Any:
        """Agent 0's session (the interactive entry)."""
        return self._lead_session

    def _role_of(self, aid: int) -> str:
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else "?"

    def _check_topology(self, src_aid: int, dst_role: str, *, verb: str) -> None:
        """Raise ``PermissionError`` if the topology forbids src → dst_role."""
        if self._topology is None:
            return
        src_role = self._role_of(src_aid)
        if not self._topology.allows(src_role, dst_role):
            raise PermissionError(
                f"Role '{src_role}' is not permitted to {verb} '{dst_role}' "
                f"under the team topology."
            )

    def team_snapshot(self) -> list[dict[str, Any]]:
        """Read-only roster of every tracked (live) agent, ordered by aid."""
        snapshot: list[dict[str, Any]] = []
        for aid in sorted(self.table.entries):
            scb = self.table.entries[aid]
            task = self._tasks.get(aid)
            snapshot.append(
                {
                    "aid": aid,
                    "role": scb.agent.name,
                    "parent_aid": scb.parent_aid,
                    "phase": scb.state.phase.value,
                    "busy": task is not None and not task.done(),
                }
            )
        return snapshot

    def team_roster(self) -> list[dict[str, Any]]:
        """Full configured team for the prompt toolbar: every live agent plus
        each configured role that has no live agent yet (``aid=None``, phase
        ``"available"``). Unlike ``team_snapshot`` (live agents only, used to
        message teammates by aid), this surfaces the team the user defined in
        the team config before anything has spawned.
        """
        live = self.team_snapshot()
        live_roles = {entry["role"] for entry in live}
        available = [
            {
                "aid": None,
                "role": role,
                "parent_aid": None,
                "phase": "available",
                "busy": False,
            }
            for role in self._roles
            if role not in live_roles
        ]
        return live + available

    async def _append_worktree_diff(self, env: EnvironmentPort, result: str) -> str:
        """If env is a worktree, append its diff to the result."""
        if not isinstance(env, DiffCapablePort):
            return result
        diff = await env.get_diff()
        if not diff:
            return result
        if len(diff) > WORKTREE_DIFF_MAX_CHARS:
            diff = (
                diff[:WORKTREE_DIFF_KEEP_CHARS]
                + f"\n\n... [{len(diff) - WORKTREE_DIFF_MAX_CHARS} chars truncated] ...\n\n"
                + diff[-WORKTREE_DIFF_KEEP_CHARS:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit a pre-built scheduler event via the event sink.

        Events are built through ``self._events`` (a ``SchedulerEventFactory``)
        so the orchestration vocabulary lives in one place; the sink is a
        required ``EventPublisherPort``, so emission is a single ``emit``.
        """
        await self._event_sink.emit(event)

    async def _safe_emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit an observational lifecycle event without changing scheduler state."""
        try:
            await self.emit_scheduler_event(event)
        except Exception as exc:
            logger.error("scheduler event %s failed: %s", event.type, exc)

    async def run(self, user_message: str) -> str:
        """Send message to lead and run until the whole team is quiescent.

        A session that suspends on deferred work (``AWAITING_EVENTS``) returns
        its task while its children run; a child's completion re-activates it
        with a fresh task. So "all tasks done" is no longer the end — the team
        is finished only when no task is running, no pending table is
        outstanding, and every session is terminal or idle (``_quiescent``).
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks.add(current_task)
        try:
            async with self._run_lock:
                return await self._run_exclusive(user_message)
        except asyncio.CancelledError:
            # The public caller owns the whole team turn. Do not leave its lead
            # driver and descendants running after that owner is cancelled.
            if not self._shutting_down:
                if current_task is not None:
                    self._active_run_tasks.discard(current_task)
                await self.cleanup()
            raise
        finally:
            if current_task is not None:
                self._active_run_tasks.discard(current_task)

    async def _run_exclusive(self, user_message: str) -> str:
        """Drive one externally visible lead turn under ``_run_lock``."""
        if self._lead_session is None:
            raise RuntimeError("Scheduler has no lead session. Call create_init_process() first.")
        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")

        # A durable restore may reopen a prior turn with completed failure rows
        # for children that cannot survive process restart. Finish that turn
        # before appending a new user message, preserving tool-call ordering.
        if self._lead_session.state.phase is SessionPhase.AWAITING_EVENTS:
            task = self._tasks.get(0)
            if task is None or task.done():
                self._reserve_turn_budget(0)
                self._tasks[0] = asyncio.create_task(
                    self._drive_agent(0, self._lead_session)
                )
            await self.wait_until_terminal(0)

        # Restored teammate messages are scheduler-owned turns. Deliver and
        # finish them before accepting the new external user turn.
        if self._message_inbox.get(0):
            await self._drain_message_inbox(0)
            await self.wait_until_terminal(0)

        if self._shutting_down:
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        turn_start = len(self._lead_session.state.messages)
        prior_lease = self._current_turn_budget(0)
        state_snapshot = copy.deepcopy(self._lead_session.state.__dict__)
        self._reserve_turn_budget(0)
        record: dict[str, Any] = {
            "aid": 0,
            "session": self._lead_session,
            "inbox": [],
            "messages": [],
            "state_snapshot": state_snapshot,
            "prior_lease": prior_lease,
            "lease_restored": False,
            "invalidated": False,
            "committed": False,
            "callback_attached": False,
            "cleanup_terminal": False,
        }
        add_task = asyncio.create_task(
            self._lead_session.add_user_message(user_message)
        )
        record["add_task"] = add_task
        self._lead_turn_record = record
        try:
            await asyncio.shield(add_task)
        except BaseException:
            self._rollback_message_delivery_locked(0, record)
            if not add_task.done():
                add_task.cancel()
            self._attach_late_message_restore(0, record, add_task)
            raise
        if self._shutting_down or record.get("invalidated", False):
            self._rollback_message_delivery_locked(0, record)
            self._attach_late_message_restore(0, record, add_task)
            raise RuntimeError("Cannot run scheduler: scheduler is shutting down.")
        record["committed"] = True
        if self._lead_turn_record is record:
            self._lead_turn_record = None
        self._tasks[0] = asyncio.create_task(self._drive_agent(0, self._lead_session))

        while True:
            for done_aid in [a for a, t in self._tasks.items() if t.done()]:
                task = self._tasks.pop(done_aid)
                try:
                    task.result()
                except asyncio.CancelledError:
                    logger.debug("background task for aid %s was cancelled", done_aid)
                except Exception as exc:
                    logger.error(
                        "background task for aid %s failed: %s", done_aid, exc
                    )
            pending = list(self._tasks.values())
            if not pending:
                if self._quiescent():
                    break
                # All tasks drained but a wake's resume task may be mid-creation
                # (or a pending table is still open) — yield and re-check rather
                # than exit early. Non-empty pending tables keep us looping
                # without busy-spinning.
                await asyncio.sleep(0)
                continue
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        self._write_manifest()

        # Limit the answer lookup to messages appended during this invocation.
        # This keeps the public return value free of the worktree diff stored on
        # the SCB and prevents a precheck-only turn from leaking an old answer.
        for message in reversed(self._lead_session.state.messages[turn_start:]):
            if message.get("role") == "assistant" and message.get("content"):
                return message["content"]
        return ""

    def _quiescent(self) -> bool:
        """True when no session is mid-flight: none is awaiting events, none has
        an outstanding pending table, and every phase is terminal or idle.
        """
        for scb in self.table.entries.values():
            if self._message_inbox.get(scb.aid):
                return False
            if not scb.state.pending_events.is_empty():
                return False
            if not (scb.state.phase.is_terminal() or scb.state.phase is SessionPhase.IDLE):
                return False
        return True

    async def cleanup(
        self,
        *,
        cleanup_timeout: float = DEFAULT_SCHEDULER_CLEANUP_TIMEOUT,
    ) -> None:
        """Cancel pending tasks and clean up worktree environments."""
        try:
            phase_timeout = float(cleanup_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "cleanup_timeout must be a finite number greater than zero"
            ) from exc
        if (
            isinstance(cleanup_timeout, bool)
            or not math.isfinite(phase_timeout)
            or phase_timeout <= 0
        ):
            raise ValueError(
                "cleanup_timeout must be a finite number greater than zero"
            )
        forced_timeout = min(
            MAX_FORCED_CLEANUP_TIMEOUT,
            max(0.1, phase_timeout),
        )

        # Validation precedes task creation so an invalid call is entirely
        # side-effect free. Concurrent cleanup callers share one teardown task;
        # cancellation of any waiter cannot cancel the resource owner.
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_impl(
                    phase_timeout=phase_timeout,
                    forced_timeout=forced_timeout,
                )
            )

        cancellation: asyncio.CancelledError | None = None
        cleanup_failure: BaseException | None = None
        while True:
            try:
                await asyncio.shield(self._cleanup_task)
                break
            except asyncio.CancelledError as exc:
                if self._cleanup_task.cancelled():
                    raise
                if cancellation is None:
                    cancellation = exc
                # Finish the already-bounded teardown before propagating caller
                # cancellation. A repeated caller cancellation is recorded by
                # the same loop and still cannot interrupt cleanup ownership.
                continue
            except BaseException as exc:
                cleanup_failure = exc
                break
        if cancellation is not None:
            if cleanup_failure is not None:
                add_note = getattr(cancellation, "add_note", None)
                if callable(add_note):
                    add_note(
                        "scheduler cleanup also failed: "
                        f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                    )
            raise cancellation
        if cleanup_failure is not None:
            raise cleanup_failure

    async def _cleanup_impl(
        self,
        *,
        phase_timeout: float,
        forced_timeout: float,
    ) -> None:
        # This is the first teardown mutation. It runs in an owned task shielded
        # from every cleanup caller, including a caller cancelled mid-wait.
        self._shutting_down = True
        cleanup_failures: list[str] = []
        persistence_sessions = tuple(self._sessions.values())
        startup_aids = set(self._startup_tasks)
        cleanup_origin_snapshot = {
            **self._startup_origin,
            **self._spawn_origin,
        }
        execution_tasks = [
            *self._tasks.items(),
            *self._startup_tasks.items(),
        ]
        delivery_records = list(self._message_delivery_records.values())
        if self._lead_turn_record is not None:
            self._lead_turn_record["cleanup_terminal"] = True
            delivery_records.append(self._lead_turn_record)
        delivery_tasks = [
            *self._message_delivery_tasks.items(),
            *((0, task) for task in self._active_run_tasks),
        ]
        for record in delivery_records:
            add_task = record.get("add_task")
            if isinstance(add_task, asyncio.Task):
                delivery_tasks.append((int(record["aid"]), add_task))
        tracked_tasks = [*execution_tasks, *delivery_tasks]
        for task in {task for _, task in tracked_tasks}:
            if not task.done():
                task.cancel()

        await self._rollback_message_deliveries(
            delivery_records,
            timeout=forced_timeout,
        )

        pending = await self._wait_for_cleanup_tasks(
            {task for _, task in tracked_tasks},
            timeout=phase_timeout,
        )
        execution_required_forced_stop = False
        forced_execution_aids: set[int] = set()
        environment_abort_succeeded = True
        if pending:
            pending_aids = {
                aid for aid, task in execution_tasks if task in pending
            }
            if pending_aids:
                environment_abort_succeeded = await self._abort_session_environments(
                    pending_aids,
                    timeout=forced_timeout,
                )
            for task in pending:
                task.cancel()
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=forced_timeout,
            )
        if pending:
            execution_required_forced_stop = True
            forced_execution_aids = {
                aid for aid, task in execution_tasks if task in pending
            }
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(
                    task,
                    timeout=forced_timeout,
                )
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error("forced scheduler task termination failed: %s", error)
            pending = still_pending
        if execution_required_forced_stop:
            cleanup_failures.append("execution tasks did not quiesce")
        if not environment_abort_succeeded:
            cleanup_failures.append("session environment abort failed or timed out")

        await self._rollback_message_deliveries(
            delivery_records,
            timeout=forced_timeout,
        )

        # A Task cancelled before its coroutine gets its first timeslice never
        # enters ``_drive_agent``'s CancelledError handler. The same is true for
        # a stubborn task still alive after both bounded phases. Finalize both
        # states here so teardown cannot leave a scheduled ghost with a live
        # lease or an unresolved parent row.
        for aid, task in execution_tasks:
            if not (
                aid in forced_execution_aids
                or task in pending
                or task.cancelled()
                or self._task_finished_with_error(task)
            ):
                continue
            self._finalize_cleanup_failure(
                aid,
                origin_override=cleanup_origin_snapshot.get(aid),
            )
        for task in pending:
            task.add_done_callback(self._consume_background_task)
        for aid in startup_aids:
            self._finalize_cleanup_failure(
                aid,
                origin_override=cleanup_origin_snapshot.get(aid),
            )
            self.table.entries.pop(aid, None)
            self._sessions.pop(aid, None)
            self._locks.pop(aid, None)
            self._message_inbox.pop(aid, None)
        self._startup_tasks.clear()
        self._startup_envs.clear()
        self._startup_origin.clear()
        for record in delivery_records:
            aid = int(record["aid"])
            self._message_delivery_tasks.pop(aid, None)
            if self._message_delivery_records.get(aid) is record:
                self._message_delivery_records.pop(aid, None)
            if self._lead_turn_record is record:
                self._lead_turn_record = None
            add_task = record.get("add_task")
            if isinstance(add_task, asyncio.Task):
                self._attach_late_message_restore(aid, record, add_task)
            if record.get("cleanup_terminal", False):
                self._finalize_cleanup_failure(aid)
        self._active_run_tasks.clear()
        # Teardown is terminal for this scheduler instance. Release even a
        # seeded-but-never-run Lead lease and defensively clear any reservation /
        # dedup entry whose owner disappeared before it entered a tracked task.
        self._lead_reservation = None
        self._child_reservation.clear()
        self._reservation_baseline.clear()
        self._inflight.clear()
        self._inflight_key_of.clear()
        self._tasks.clear()

        persistence_quiesced = await self._wait_for_session_persistence(
            persistence_sessions,
            timeout=phase_timeout,
        )
        if not persistence_quiesced:
            for task in self._pending_session_persistence_tasks(
                persistence_sessions
            ):
                task.cancel()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=forced_timeout,
            )

        if persistence_quiesced:
            self._autosave_all_sessions()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=phase_timeout,
            )
            if not persistence_quiesced:
                for task in self._pending_session_persistence_tasks(
                    persistence_sessions
                ):
                    task.cancel()
                persistence_quiesced = await self._wait_for_session_persistence(
                    persistence_sessions,
                    timeout=forced_timeout,
                )
        if persistence_quiesced:
            self._write_manifest()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=phase_timeout,
            )
            if not persistence_quiesced:
                for task in self._pending_session_persistence_tasks(
                    persistence_sessions
                ):
                    task.cancel()
                persistence_quiesced = await self._wait_for_session_persistence(
                    persistence_sessions,
                    timeout=forced_timeout,
                )
        persistence_errors = self._session_persistence_errors(
            persistence_sessions
        )
        worktree_release_safe = (
            not execution_required_forced_stop
            and not pending
            and environment_abort_succeeded
        )
        if worktree_release_safe:
            worktree_release_succeeded = await self._release_worktree_pool_bounded(
                cleanup_timeout=phase_timeout,
                forced_timeout=forced_timeout,
            )
        else:
            worktree_release_succeeded = False
        if not persistence_quiesced:
            cleanup_failures.append("session-owned tasks did not quiesce")
        if persistence_errors:
            cleanup_failures.append("session persistence failed")
        if not worktree_release_succeeded:
            if worktree_release_safe:
                cleanup_failures.append("worktree pool release failed or timed out")
            else:
                cleanup_failures.append(
                    "worktree pool release skipped because execution ownership "
                    "was not revoked and quiesced"
                )
        if cleanup_failures:
            failure = RuntimeError(
                "technical scheduler cleanup failed: "
                + "; ".join(cleanup_failures)
            )
            if persistence_errors:
                raise failure from persistence_errors[0]
            raise failure

    @staticmethod
    async def _wait_for_cleanup_tasks(
        tasks: set[asyncio.Task[Any]], *, timeout: float
    ) -> set[asyncio.Task[Any]]:
        pending = {task for task in tasks if not task.done()}
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=max(0.0, timeout),
            )
        for task in tasks - pending:
            Scheduler._consume_background_task(task)
        return pending

    def _persistence_sessions(
        self,
        initial: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        sessions: list[Any] = []
        seen: set[int] = set()
        for session in (*initial, *self._sessions.values()):
            if id(session) in seen:
                continue
            seen.add(id(session))
            sessions.append(session)
        return tuple(sessions)

    def _pending_session_persistence_tasks(
        self,
        sessions: tuple[Any, ...],
    ) -> set[asyncio.Task[Any]]:
        pending: set[asyncio.Task[Any]] = set()
        current = asyncio.current_task()
        for session in self._persistence_sessions(sessions):
            for task in getattr(session, "pending_cleanup_tasks", ()):
                if (
                    isinstance(task, asyncio.Task)
                    and not task.done()
                    and task is not current
                ):
                    pending.add(task)
        for subscriber in self._fallback_autosavers.values():
            pending.update(subscriber.pending_tasks)
        if self._manifest_subscriber is not None:
            pending.update(self._manifest_subscriber.pending_tasks)
        return pending

    async def _wait_for_session_persistence(
        self,
        sessions: tuple[Any, ...],
        *,
        timeout: float,
    ) -> bool:
        """Wait through one stable empty turn for subscriber-owned saves."""
        deadline = asyncio.get_running_loop().time() + timeout
        saw_empty = False
        while True:
            pending = self._pending_session_persistence_tasks(sessions)
            if not pending:
                if saw_empty:
                    return True
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            _done, still_pending = await asyncio.wait(
                pending,
                timeout=remaining,
            )
            if still_pending:
                return False

    def _session_persistence_errors(
        self,
        sessions: tuple[Any, ...],
    ) -> tuple[Exception, ...]:
        errors = list(self._scheduler_persistence_errors)
        for session in self._persistence_sessions(sessions):
            for error in getattr(session, "persistence_errors", ()):
                if isinstance(error, Exception):
                    errors.append(error)
        for subscriber in self._fallback_autosavers.values():
            if subscriber.last_error is not None:
                errors.append(subscriber.last_error)
        if (
            self._manifest_subscriber is not None
            and self._manifest_subscriber.last_error is not None
        ):
            errors.append(self._manifest_subscriber.last_error)
        return tuple(errors)

    @staticmethod
    def _consume_background_task(task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _task_finished_with_error(task: asyncio.Task[Any]) -> bool:
        if not task.done():
            return False
        try:
            return task.exception() is not None
        except asyncio.CancelledError:
            return True

    async def _rollback_message_deliveries(
        self,
        records: list[dict[str, Any]],
        *,
        timeout: float,
    ) -> None:
        async def rollback(record: dict[str, Any]) -> None:
            aid = int(record["aid"])
            lock = self._locks.setdefault(aid, asyncio.Lock())
            async with lock:
                self._rollback_message_delivery_locked(aid, record)

        rollback_tasks = {
            asyncio.create_task(rollback(record))
            for record in records
            if not record.get("committed", False)
        }
        pending = await self._wait_for_cleanup_tasks(
            rollback_tasks,
            timeout=timeout,
        )
        for task in pending:
            task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=timeout,
            )
        if pending:
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(task, timeout=timeout)
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error(
                        "forced message rollback termination failed: %s",
                        error,
                    )
            pending = still_pending
        for task in pending:
            task.add_done_callback(self._consume_background_task)

    def _finalize_cleanup_failure(
        self,
        aid: int,
        *,
        origin_override: tuple[int, str] | None = None,
    ) -> None:
        reason = "Error: scheduler cleanup cancelled delegated work"
        self._release_reservations(aid)
        if aid in self._delivery_committed:
            return
        origin = self._spawn_origin.pop(aid, None)
        if origin is None:
            origin = self._startup_origin.pop(aid, None)
        if origin is None:
            origin = origin_override
        if origin is not None:
            parent_aid, tool_call_id = origin
            parent = self.table.get(parent_aid)
            if (
                parent is not None
                and tool_call_id in parent.state.pending_events.rows
            ):
                try:
                    parent.state.pending_events.fill(
                        tool_call_id,
                        result=reason,
                        status=RowStatus.FAILED,
                        error=reason,
                    )
                except PendingRowError:
                    logger.debug(
                        "cleanup could not fail pending row %s on aid %s",
                        tool_call_id,
                        parent_aid,
                    )
        scb = self.table.get(aid)
        if scb is not None:
            scb.state.cancel(reason)
            scb.result = reason

    async def _abort_session_environments(
        self, aids: set[int], *, timeout: float
    ) -> bool:
        seen: set[int] = set()
        abort_tasks: set[asyncio.Task[Any]] = set()
        succeeded = True
        for aid in aids:
            session = self._sessions.get(aid)
            envs = [self._startup_envs.get(aid)]
            if session is not None:
                envs.append(getattr(session, "env", None))
                tool_execution = getattr(session, "tool_execution", None)
                envs.append(getattr(tool_execution, "environment", None))
            for env in envs:
                if env is None or id(env) in seen:
                    continue
                seen.add(id(env))
                try:
                    env._aborted = True
                except BaseException as exc:
                    succeeded = False
                    logger.error(
                        "session environment synchronous revoke failed for aid %s: %s",
                        aid,
                        exc,
                    )
                abort = getattr(env, "abort", None)
                if not callable(abort):
                    continue
                try:
                    result = abort()
                except BaseException as exc:
                    succeeded = False
                    logger.error("session environment abort failed for aid %s: %s", aid, exc)
                    continue
                if inspect.isawaitable(result):
                    try:
                        abort_tasks.add(asyncio.ensure_future(result))
                    except BaseException as exc:
                        succeeded = False
                        logger.error(
                            "session environment abort scheduling failed for aid %s: %s",
                            aid,
                            exc,
                        )
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()

        pending = await self._wait_for_cleanup_tasks(abort_tasks, timeout=timeout)
        if pending:
            succeeded = False
        for task in pending:
            task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=timeout,
            )
        if pending:
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(task, timeout=timeout)
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error(
                        "forced session environment abort termination failed: %s",
                        error,
                    )
            pending = still_pending
        if pending:
            logger.error(
                "%s session environment abort task(s) remained active after cleanup timeout",
                len(pending),
            )
        for task in abort_tasks:
            if not task.done():
                task.add_done_callback(self._consume_background_task)
                continue
            if task.cancelled():
                succeeded = False
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                succeeded = False
            else:
                if error is not None:
                    succeeded = False
                    logger.error("session environment abort task failed: %s", error)
        return succeeded and not pending

    async def _release_worktree_pool_bounded(
        self,
        *,
        cleanup_timeout: float,
        forced_timeout: float,
    ) -> bool:
        try:
            result = self._worktree_pool.release()
        except BaseException as exc:
            logger.error("worktree pool release failed: %s", exc)
            return False
        if not inspect.isawaitable(result):
            return True
        try:
            task = asyncio.ensure_future(result)
        except BaseException as exc:
            logger.error("worktree pool release scheduling failed: %s", exc)
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return False
        pending = await self._wait_for_cleanup_tasks(
            {task},
            timeout=cleanup_timeout,
        )
        succeeded = not pending
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=forced_timeout,
            )
        if pending:
            termination = await force_task_terminal(
                task,
                timeout=forced_timeout,
            )
            pending = set() if termination.terminal else {task}
            for error in termination.errors:
                logger.error("forced worktree release termination failed: %s", error)
        for pending_task in pending:
            pending_task.add_done_callback(self._consume_background_task)
        if pending:
            logger.error("worktree pool release remained active after cleanup timeout")
            return False
        if task.cancelled():
            return False
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return False
        if error is not None:
            logger.error("worktree pool release failed: %s", error)
            return False
        return succeeded

    async def wait_for(self, aid: int) -> None:
        """Wait until ``aid``'s current driving task settles.

        Returns immediately when no task is tracked for ``aid``. A session
        that suspends on deferred work returns its task while its children
        run, so this waits for the current task only, not for the session to
        reach a terminal phase.
        """
        task = self._tasks.get(aid)
        if task is not None:
            await asyncio.shield(task)

    async def wait_until_terminal(self, aid: int) -> None:
        """Wait through every suspend/resume cycle until ``aid`` is terminal.

        A driving task can finish while the session is only awaiting a nested
        child. Follow replacement tasks installed by ``_wake`` so callers such
        as the review loop never inspect an intermediate SCB result.
        """
        while True:
            scb = self.table.get(aid)
            if scb is None:
                raise LookupError(f"Unknown agent id: {aid}")

            task = self._tasks.get(aid)
            if scb.state.phase.is_terminal():
                if task is not None and not task.done():
                    await asyncio.shield(task)
                    continue
                # A finishing task may have installed a message-driven
                # replacement after the terminal phase was first observed.
                if self._tasks.get(aid) is not task:
                    continue
                return

            if task is not None and not task.done():
                await asyncio.shield(task)
            else:
                # A child completion may be between filling the pending table
                # and publishing the replacement driving task.
                await asyncio.sleep(0)

    async def spawn_with_review(
        self,
        parent_aid: int,
        task: str,
        context: str = "",
        max_iterations: int = 3,
    ) -> str:
        """Self-Collaboration: Coder -> Reviewer loop (see ``self_collaboration``)."""
        tracker = {"outstanding": 0}
        token = self._review_parent_lease_tracker.set((parent_aid, tracker))
        try:
            return await run_spawn_with_review(
                self, parent_aid, task, context, max_iterations
            )
        finally:
            self._review_parent_lease_tracker.reset(token)
            if (
                tracker["outstanding"] > 0
                and not self._shutting_down
                and self.table.get(parent_aid) is not None
            ):
                self._reserve_turn_budget(parent_aid)


__all__ = ["LaunchSpec", "QueuedTeammateMessage", "Scheduler"]
