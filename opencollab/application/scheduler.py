"""Cooperative team scheduler over shared Session primitives.

Mixins own lifecycle, messaging, budgets, persistence, team views, cleanup,
and review waits. This module owns construction and the public import surface.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Awaitable, Callable, Mapping

from opencollab.application._scheduler_cleanup import SchedulerCleanupMixin
from opencollab.application._scheduler_constants import DEFAULT_SCHEDULER_CLEANUP_TIMEOUT
from opencollab.application._scheduler_delivery_tree import SchedulerDeliveryTreeMixin
from opencollab.application._scheduler_persistence import SchedulerPersistenceMixin
from opencollab.application._scheduler_review import SchedulerReviewMixin
from opencollab.application._scheduler_run import SchedulerRunMixin
from opencollab.application._scheduler_stop_notice import SchedulerStopNoticeMixin
from opencollab.application._scheduler_team import SchedulerTeamMixin, _CompletedCoderChanges
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
    WorkingTreeProbe,
    WorktreePoolPort,
)
from opencollab.application.scheduler_dedup import InflightDedupMixin
from opencollab.application.scheduler_lifecycle import LifecycleMixin, _SerializedTurn
from opencollab.application.scheduler_messaging import MessagingMixin
from opencollab.application.scheduler_types import DuplicateSpawnError as DuplicateSpawnError
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
    SchedulerDeliveryTreeMixin,
    SchedulerReviewMixin,
    SchedulerStopNoticeMixin,
    LifecycleMixin,
    MessagingMixin,
    InflightDedupMixin,
):
    """Track sessions, spawn children, and deliver results through deferred joins.

    Spawn failures roll back budget leases, environments, and registration.
    An AWAITING_EVENTS parent resumes when its whole result batch is ready.
    ``prebuild_team`` seats the declared roster before any model call and then
    refuses spawning; otherwise the model determines the roster during the run.
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
        prebuild_team: bool = False,
        serialize_turns: bool = False,
        delivery_tree_probe: WorkingTreeProbe | None = None,
        role_budgets: Mapping[str, int] | None = None,
        entry_role: str | None = None,
        candidate_diff_writer: Callable[[int, str, str], Awaitable[str]] | None = None,
        cleanup_timeout: float = DEFAULT_SCHEDULER_CLEANUP_TIMEOUT,
    ):
        self.preserve_entry_changes = False
        self._cleanup_timeout = self._validate_cleanup_timeout(cleanup_timeout)
        self.entry_patch_path: str | None = None
        self._session_factory = session_factory
        self._worktree_pool = worktree_pool
        self._event_sink = event_sink
        self._events = event_factory or default_scheduler_event_factory()
        self._tracer = tracer
        self._max_budget_tokens = max_budget_tokens
        self._permission_policy = permission_policy
        self._topology = topology
        # Static-topology mode. Off by default, and every behavioural difference
        # is gated on it: the team is materialized from the config before the
        # first turn (``ensure_team_prebuilt``), ``spawn`` then refuses, and the
        # assigned topology is recorded. Left off, nothing below fires and the
        # scheduler behaves exactly as it did.
        self._prebuild_team = bool(prebuild_team)
        self._team_prebuilt = False
        # One turn at a time across the whole team. Off by default, which is the
        # concurrent behaviour above. On, ``_turn_gate`` holds a single lock
        # while a driver executes its model/tool work. A synchronous review
        # yields that driver's execution right while waiting for its children,
        # then reacquires before the parent continues. A teammate woken by a
        # message waits for the lock. What the agents may say
        # to each other is untouched: the topology keeps every declared edge and
        # ``message_agent`` stays voluntary — only the timing changes.
        self._serialize_turns = bool(serialize_turns)
        self._delivery_tree_probe = delivery_tree_probe
        self._delivery_tree_snapshots: list[dict[str, Any]] = []
        self._delivery_tree_shas: dict[str, int] = {}
        # Created on first use: ``__init__`` may run without a running loop.
        self._prebuild_lock: asyncio.Lock | None = None
        self._turn_gate_lock: asyncio.Lock | None = None
        # Tool execution runs in a child task and inherits its driver's context.
        # The holder records which driver owns the shared execution lock.
        self._serialized_turn: contextvars.ContextVar[_SerializedTurn | None] = (
            contextvars.ContextVar("serialized_turn", default=None)
        )
        # Drivers waiting to enter the existing serialized turn lock.
        self._turn_waiters: set[int] = set()
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
        # Independent per-role allowances from the team file, or ``{}`` for the
        # shared-pool rule. Each agent is held to its own role's allowance and
        # to nothing else; see ``_agent_cap``.
        self._role_budgets: dict[str, int] = dict(role_budgets or {})
        self._entry_role = entry_role
        if self._role_budgets:
            if not self._prebuild_team:
                raise ValueError(
                    "per-role token budgets need a declared roster (prebuild_team): "
                    "a role a model spawns mid-run has no allowance to be held to"
                )
            missing = [role for role in self._roles if role not in self._role_budgets]
            if missing or entry_role not in self._role_budgets:
                raise ValueError(
                    f"per-role token budgets must cover every declared role; missing {missing or [entry_role]}"
                )

        self.table = SessionTable()
        self._tasks: dict[int, asyncio.Task] = {}
        # Monotonic start of the current agent turn. A deferred suspend keeps
        # this entry so terminal latency includes child wait and resume work.
        self._turn_started_at: dict[int, float] = {}
        # Optional cooperative cancellation token owned by the current public
        # turn for each aid. Deferred resume drivers reuse the same token.
        self._turn_cancel_events: dict[int, asyncio.Event] = {}
        # ``spawn`` owns resources before the child driver exists. Track that
        # pre-driver window separately so cleanup can cancel/abort/finalize an
        # external spawn blocked in worktree setup or lifecycle event delivery.
        self._startup_tasks: dict[int, asyncio.Task[Any]] = {}
        self._startup_envs: dict[int, Any] = {}
        self._startup_origin: dict[int, tuple[int, str]] = {}
        self._sessions: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # Public turns serialize per addressed session, not across the whole
        # team. Independent aids may make progress concurrently while repeat
        # turns for one aid remain strictly ordered.
        self._run_locks: dict[int, asyncio.Lock] = {}
        # A public run_turn owner must retain its addressed aid through cleanup:
        # cancelling a child turn must never terminalize the Lead by default.
        self._active_run_tasks: dict[asyncio.Task[Any], int] = {}
        # An inbox follow-up can replace the addressed session's terminal state.
        # None leaves transcript lookup available. An empty string records an answerless checkpoint failure.
        self._completed_public_turn_answers: dict[int, str | None] = {}
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
        # Full successful child changes precede the bounded text sent to a
        # parent. The parent's live turn identity keeps adoption in that turn.
        self._completed_coder_diffs: dict[int, _CompletedCoderChanges] = {}
        self._adopted_coders: dict[int, _CompletedCoderChanges] = {}
        self._adoption_lock = asyncio.Lock()
        self._candidate_diff_writer = candidate_diff_writer
        # Single-flight spawn dedup: delegated-work identity -> the handling aid,
        # plus the reverse map for release. A (parent, role, task, context) is
        # reserved at spawn and freed when the child reaches a terminal phase.
        self._inflight: dict[str, int] = {}
        self._inflight_key_of: dict[int, str] = {}
        # Per-active-turn token leases — one table for every agent, agent 0
        # included. A lease records the session's token count when the grant was
        # made, so consumed tokens replace reserved headroom instead of being
        # counted twice. Releasing a terminal turn returns only its *unspent*
        # grant; tokens already consumed remain in ``used_tokens``.
        self._turn_lease: dict[int, int] = {}
        self._lease_baseline: dict[int, int] = {}
        # Dynamic-roster leases whose provider generation outlived its driver
        # are held by cleanup-task group until late usage has been reconciled.
        self._pending_provider_budget_leases: dict[
            object,
            tuple[
                int,
                int,
                int,
                frozenset[asyncio.Task[Any]],
                set[asyncio.Task[Any]],
            ],
        ] = {}
        # aid -> queued teammate messages waiting to be appended as user
        # messages once that session is not running or suspended on pending work.
        self._message_inbox: dict[int, list[QueuedTeammateMessage]] = {}
        # Targeted cancellation holds queued teammate messages until the next
        # public run for that aid; ordinary terminal agents still wake to inboxes.
        self._cancelled_turn_inbox_holds: set[int] = set()
        # recipient -> sender -> latest unanswered teammate message id
        self._unanswered: dict[int, dict[int, str]] = {}
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
        self._pending_shutdown_tasks: set[asyncio.Future[object]] = set()
        self._deferred_turn_cleanup: asyncio.Task[None] | None = None
        self._session_close_task: asyncio.Task[bool] | None = None
        self._worktree_release_task: asyncio.Task[None] | None = None
        self._deferred_worktree_release: asyncio.Task[None] | None = None
        self._fallback_autosavers: dict[int, AutoSaveSubscriber] = {}
        self._scheduler_persistence_errors: list[Exception] = []
        # Bootstrap-owned resources whose process/persistence state must be
        # proven quiescent before scheduler cleanup may release worktrees.
        self._lifecycle_resources: list[tuple[str, Any]] = []
        # A synchronous review loop temporarily yields its caller's turn lease
        # through ordinary ``spawn`` calls. Context-local accounting records only
        # leases actually released by this review invocation, so an external API
        # call that never suspended its parent cannot accidentally expand the
        # Lead reservation when the review returns.
        self._review_parent_lease_tracker: contextvars.ContextVar[
            tuple[int, dict[str, int]] | None
        ] = contextvars.ContextVar("review_parent_lease_tracker", default=None)

    def register_lifecycle_resource(
        self,
        resource: Any,
        *,
        description: str,
    ) -> None:
        """Make an externally owned resource part of final cleanup evidence."""
        self._lifecycle_resources.append((description, resource))


__all__ = [
    "DuplicateSpawnError",
    "LaunchSpec",
    "QueuedTeammateMessage",
    "Scheduler",
    "SchedulerStalledError",
    "SchedulerTurnError",
]
