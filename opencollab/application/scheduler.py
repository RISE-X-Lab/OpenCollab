"""Scheduler — passive process tracker for multi-agent execution.

Tracks every Session as a SessionControlBlock in a SessionTable:
- spawn() is non-blocking (returns aid immediately)
- Agents run in parallel via asyncio.create_task
- Results are delivered to parents via message injection into parent state

The class is assembled from cohesive application-layer mixins:
- focused local mixins cover persistence/budgets, team views, the public run
  loop, bounded cleanup, and review waits
- ``LifecycleMixin`` (``scheduler_lifecycle``) — register/spawn/drive/wake/deliver
- ``MessagingMixin`` (``scheduler_messaging``) — teammate-message inbox
- ``InflightDedupMixin`` (``scheduler_dedup``) — single-flight spawn dedup

This module keeps construction and the public import surface.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Callable

from opencollab.application._scheduler_cleanup import SchedulerCleanupMixin
from opencollab.application._scheduler_persistence import SchedulerPersistenceMixin
from opencollab.application._scheduler_review import SchedulerReviewMixin
from opencollab.application._scheduler_run import SchedulerRunMixin
from opencollab.application._scheduler_team import SchedulerTeamMixin
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.events import (
    SchedulerEventFactory,
    default_scheduler_event_factory,
)
from opencollab.application.ports import (
    EventPublisherPort,
    PermissionPort,
    SessionFactoryPort,
    TracePort,
    WorktreePoolPort,
)
from opencollab.application.scheduler_dedup import InflightDedupMixin
from opencollab.application.scheduler_lifecycle import LifecycleMixin
from opencollab.application.scheduler_messaging import MessagingMixin
from opencollab.application.scheduler_types import LaunchSpec as LaunchSpec
from opencollab.application.scheduler_types import QueuedTeammateMessage as QueuedTeammateMessage
from opencollab.application.scheduler_types import SchedulerStalledError as SchedulerStalledError
from opencollab.application.scheduler_types import SchedulerTurnError as SchedulerTurnError
from opencollab.domain.identity import role_collision_key, validate_role_identity
from opencollab.domain.scheduler import SessionTable
from opencollab.domain.team import Topology


class Scheduler(
    SchedulerPersistenceMixin,
    SchedulerTeamMixin,
    SchedulerRunMixin,
    SchedulerCleanupMixin,
    SchedulerReviewMixin,
    LifecycleMixin,
    MessagingMixin,
    InflightDedupMixin,
):
    """Passive scheduler that tracks SCBs and runs agents in parallel.

    Design:
    - SessionTable holds all SCBs (pure data, no I/O)
    - spawn() creates a new SCB and schedules it for execution
    - run_turn() addresses one agent and waits for all agents to complete
    - Results are injected into parent sessions as system messages

    One of two Strategies driving ``session.run_loop()``: this event-driven,
    LLM-supervised scheduler and the code-driven ``WorkflowContext`` are
    interchangeable regimes over the identical Session process primitive.

    OS process model — the metaphor this subsystem operationalizes:
      Session (its run_loop)       -> a process
      SessionControlBlock (SCB)    -> the process control block (PCB)
      SessionTable                 -> the process table
      aid / parent_aid             -> pid / ppid
      spawn()                      -> fork(): transactional — a failure rolls
                                      back the budget lease, worktree, and maps
                                      so a half-forked child never leaks
      budget lease                 -> a CPU/memory quota, reserved at grant
      Topology                     -> the IPC permission matrix (who may
                                      message / spawn whom)
      pending_events               -> the ready/wait queue + wakeup: an
        (AWAITING_EVENTS)             AWAITING_EVENTS parent blocks on its
                                      children and is woken when the whole
                                      batch has answered
      IDLE / run-ring /            -> process states: ready / running /
        DONE|STOPPED|ERROR            blocked / exit(0) | killed | crash
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
        self._lead_session: Any | None = None
        # child aid -> (parent aid, tool_call_id) for deferred spawns: lets a
        # child's completion fill the exact pending row that suspended its
        # parent. Absent for legacy fire-and-forget spawns (tool_call_id=None).
        self._spawn_origin: dict[int, tuple[int, str]] = {}
        # A completion can race its parent's pending-row publication. Retain its
        # origin for one bounded recovery attempt instead of silently discarding
        # the only route back to the suspended parent.
        self._delivery_route_failures: dict[int, int] = {}
        # Children whose result row was atomically filled before teardown began.
        # Cleanup may cancel their task while it is only emitting an observational
        # post-delivery event; this marker preserves the already-committed child /
        # parent terminal pair instead of rewriting only the child to CANCELLED.
        self._delivery_committed: set[int] = set()
        # Single-flight spawn dedup: delegated-work identity -> the handling aid,
        # plus the reverse map for release. A (parent, role, task, context) is
        # reserved at spawn and freed when the child reaches a terminal phase.
        self._inflight: dict[str, int] = {}
        self._inflight_key_of: dict[int, str] = {}
        # Per-active-turn token leases. A lease records the session's token count
        # when the grant was made, so consumed tokens replace reserved headroom
        # instead of being counted twice. Releasing a terminal turn returns only
        # its *unspent* grant; tokens already consumed remain in ``used_tokens``.
        # The Lead lease is separate because several arithmetic tests seed it
        # without registering a real aid=0 session.
        self._lead_lease: tuple[int, int] | None = None
        self._child_lease: dict[int, int] = {}
        self._lease_baseline: dict[int, int] = {}
        # aid -> queued teammate messages waiting to be appended as user
        # messages once that session is not running or suspended on pending work.
        self._message_inbox: dict[int, list[QueuedTeammateMessage]] = {}
        # The outer delivery task remains owned while ``add_user_message`` runs.
        # The durable inbox entry is removed only after that call succeeds.
        self._message_delivery_tasks: dict[int, asyncio.Task[Any]] = {}
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

__all__ = [
    "LaunchSpec",
    "QueuedTeammateMessage",
    "Scheduler",
    "SchedulerStalledError",
    "SchedulerTurnError",
]
