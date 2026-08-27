"""Scheduler persistence and token-budget accounting."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.events import SchedulerEventFactory
from opencollab.domain.scheduler import dynamic_roster_share, split_budget

logger = logging.getLogger(__name__)


class SchedulerPersistenceMixin:
    def _track_review_parent_lease_release(self, parent_aid: int, delta: int) -> None:
        tracker = self._review_parent_lease_tracker.get()
        if tracker is None or tracker[0] != parent_aid:
            return
        tracker[1]["outstanding"] = max(0, tracker[1].get("outstanding", 0) + delta)

    def set_manifest_writer(
        self,
        fn: Callable[[], None],
        *,
        prepare_fn: Callable[[], Callable[[], None] | None] | None = None,
    ) -> None:
        """Inject the team-manifest persister (called on every roster change)."""
        self._manifest_writer = fn
        self._manifest_subscriber = AutoSaveSubscriber(fn, prepare_fn=prepare_fn)

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
                subscriber = AutoSaveSubscriber(lambda: save(save_path))
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

    def _seed_entry_lease(self) -> None:
        """Book agent 0's own share of the pool when it registers (idempotent).

        Reserve-at-allocation divides the pool as agents appear, so agent 0 has
        to book its share before any child books one — otherwise the children
        would divide a pool nobody had left room in. It books exactly what a
        child books, ``dynamic_roster_share(total)``, and into the same lease
        table: agent 0 has no account of its own.
        """
        self._turn_lease[0] = dynamic_roster_share(self._max_budget_tokens)
        self._lease_baseline[0] = self._session_used_tokens(0)

    def _session_used_tokens(self, aid: int) -> int:
        scb = self.table.get(aid)
        if scb is not None:
            return max(0, int(getattr(scb.state, "used_tokens", 0) or 0))
        session = self._sessions.get(aid)
        return max(0, int(getattr(session, "used_tokens", 0) or 0))

    def _lease_remaining(self, aid: int, grant: int, baseline: int) -> int:
        used = self._session_used_tokens(aid)
        return max(0, grant - max(0, used - baseline))

    def _is_entry_session(self, aid: int) -> bool:
        """True when ``aid`` names the registered agent 0 (the entry session)."""
        return (
            aid == 0
            and self._lead_session is not None
            and self._sessions.get(0) is self._lead_session
        )

    def _budget_committed(self) -> int:
        committed = max(0, self.used_tokens)
        for aid, grant in self._turn_lease.items():
            committed += self._lease_remaining(aid, grant, self._lease_baseline.get(aid, 0))
        return committed

    def _reserve_child_budget(self, aid: int) -> int:
        """Grant a child its cap from the unallocated remainder and book it.

        Synchronous (no await): a duplicate / batched spawn that runs before the
        first child's await already sees the updated ``_allocated_tokens``.
        Returns the granted cap.
        """
        grant = split_budget(self._max_budget_tokens, self._budget_committed())
        self._turn_lease[aid] = grant
        self._lease_baseline[aid] = self._session_used_tokens(aid)
        return grant

    def _reserve_turn_lease(self, aid: int) -> int:
        """Lease every currently available token to a resuming session turn."""
        self._release_turn_lease(aid)
        grant = max(0, self._max_budget_tokens - self._budget_committed())
        baseline = self._session_used_tokens(aid)
        if grant > 0:
            self._turn_lease[aid] = grant
            self._lease_baseline[aid] = baseline
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
        if self._is_entry_session(aid):
            grant = self._reserve_turn_lease(aid)
        elif aid in self._turn_lease:
            return True
        else:
            grant = self._reserve_child_budget(aid)
            baseline = self._session_used_tokens(aid)
            self._set_session_budget_limit(aid, baseline + grant)
            if grant <= 0:
                self._release_turn_lease(aid)
        # When spend already reached the ceiling, run one zero-budget precheck so
        # the queued turn reaches an explicit budget terminal instead of leaving
        # a durable inbox entry that can never become runnable.
        return grant > 0 or self.budget_exhausted

    def _release_turn_lease(self, aid: int) -> tuple[int, int] | None:
        """Reclaim ``aid``'s reservation so a later turn can reuse the headroom.

        Idempotent, and the same for every agent: agent 0 is released out of the
        one lease table like any other.
        """
        grant = self._turn_lease.pop(aid, None)
        baseline = self._lease_baseline.pop(aid, None)
        if grant is None:
            return None
        return grant, baseline or 0

    def _current_turn_lease(self, aid: int) -> tuple[int, int] | None:
        """Snapshot ``aid``'s lease without mutating the shared allocation."""
        grant = self._turn_lease.get(aid)
        if grant is None:
            return None
        return grant, self._lease_baseline.get(aid, 0)

    def _restore_turn_lease(self, aid: int, lease: tuple[int, int] | None) -> None:
        if lease is None:
            return
        old_grant, old_baseline = lease
        used = self._session_used_tokens(aid)
        old_remaining = max(0, old_grant - max(0, used - old_baseline))
        available = max(0, self._max_budget_tokens - self._budget_committed())
        grant = min(old_remaining, available)
        baseline = used
        if grant > 0:
            self._turn_lease[aid] = grant
            self._lease_baseline[aid] = baseline
        self._set_session_budget_limit(aid, baseline + grant)
