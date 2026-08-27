"""Scheduler persistence and token-budget accounting."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.events import SchedulerEventFactory
from opencollab.domain.scheduler import (
    PER_AGENT_BUDGET_SHARE,
    dynamic_roster_share,
    per_agent_cap,
    split_budget,
)

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

    def _declared_team_size(self) -> int:
        """N — how many roles the team config declares, agent 0's role included."""
        return max(1, len(self._roles))

    def _per_agent_cap(self) -> int | None:
        """The most any one agent may spend, or ``None`` on a dynamic roster.

        A number only when the roster is an input to the run: the team config
        declares the roles and the scheduler was built to seat them
        (``prebuild_team``), so N is known before the first model call and the
        rule ``c * total / N`` has a divisor. See ``PER_AGENT_BUDGET_SHARE``.

        ``None`` otherwise. Without a declared roster the team is whatever the
        model spawns, N is not known while the run is still deciding it, and
        there is nothing to divide by; that path reserves ``dynamic_roster_share``
        per agent at creation instead.
        """
        if not self._prebuild_team:
            return None
        return per_agent_cap(self._max_budget_tokens, self._declared_team_size())

    def _entry_agent_takes_the_pool(self, aid: int) -> bool:
        """True where agent 0's turn is leased the whole remaining pool.

        The dynamic-roster path only, and the budget privilege the declared path
        removes. With no N there is no per-agent cap to hold agent 0 to, so its
        turn takes the entire unallocated remainder while every other agent is
        held to one ``dynamic_roster_share``. Under a declared roster agent 0 is
        leased through ``per_agent_cap`` like every teammate.
        """
        return aid == 0 and self._per_agent_cap() is None

    def _entry_start_budget(self) -> int:
        """The budget agent 0's session is built with — what it may truly spend.

        Under a declared roster that is its ``per_agent_cap``, the same figure
        every seated teammate gets. It has to be the figure it can actually
        spend: the session injects ``[Budget: ~Xk/Yk tokens left, ...]`` into the
        model's context from exactly this value on every turn, so passing the
        team total would overstate agent 0's allowance by ``N / c`` once per
        turn, for the whole run.
        """
        cap = self._per_agent_cap()
        return self._max_budget_tokens if cap is None else cap

    def _seed_entry_lease(self) -> None:
        """Book agent 0's own share of the pool when it registers (idempotent).

        Reserve-at-allocation divides the pool as agents appear, so agent 0 has
        to book its share before any child books one — otherwise the children
        would divide a pool nobody had left room in. It books exactly what a
        child books, ``dynamic_roster_share(total)``, and into the same lease
        table: agent 0 has no account of its own.

        Nothing to seed under a declared roster: the pool is shared, each agent
        is bounded by ``per_agent_cap`` instead of by a booking, and an idle
        agent 0 must hold no tokens.
        """
        if self._per_agent_cap() is not None:
            return
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
        """Tokens the team can no longer spend on something else.

        Under a declared roster that is exactly what has been spent. The pool is
        shared, and a lease there sets a ceiling rather than taking tokens out of
        it, so an agent that was seated and never used commits nothing and the
        agents that are working can still reach the whole remainder.

        Under a dynamic roster a live lease still commits its unspent remainder,
        because that reservation is the only thing stopping the next spawn from
        oversubscribing the pool.
        """
        committed = max(0, self.used_tokens)
        if self._per_agent_cap() is not None:
            return committed
        for aid, grant in self._turn_lease.items():
            committed += self._lease_remaining(aid, grant, self._lease_baseline.get(aid, 0))
        return committed

    def _reserve_child_budget(self, aid: int) -> int:
        """The budget a newly created agent's session is built with.

        Declared roster: its ``per_agent_cap``, and nothing is booked. Seating an
        agent takes no tokens out of the shared pool, so a team of any declared
        size can be seated and a teammate that is never used holds nothing.

        Dynamic roster: one ``dynamic_roster_share`` out of the unallocated
        remainder, booked here. Synchronous (no await): a duplicate / batched
        spawn that runs before the first child's await already sees the updated
        allocation and cannot oversubscribe the pool.
        """
        cap = self._per_agent_cap()
        if cap is not None:
            return cap
        grant = split_budget(self._max_budget_tokens, self._budget_committed())
        self._turn_lease[aid] = grant
        self._lease_baseline[aid] = self._session_used_tokens(aid)
        return grant

    def _grant_under_cap(self, aid: int, cap: int, used: int) -> int:
        """What the shared pool may lend ``aid`` this turn, under ``aid``'s cap.

        Two limits, and the smaller one wins: what the team has not spent, and
        what this agent has left of its own allowance. Nothing was set aside for
        anyone at creation, so the first number is simply ``total - spent`` — an
        agent whose teammates stayed idle can draw far past an equal share. The
        second is measured against *cumulative* spend, so the cap bounds the run,
        not the turn.
        """
        agent_remaining = cap - used
        pool_remaining = self._max_budget_tokens - self.used_tokens
        if agent_remaining <= 0 < pool_remaining:
            self._trace_agent_cap_reached(
                aid,
                cap=cap,
                used=used,
                pool_remaining=pool_remaining,
            )
        return max(0, min(agent_remaining, pool_remaining))

    def _trace_agent_cap_reached(
        self,
        aid: int,
        *,
        cap: int,
        used: int,
        pool_remaining: int,
    ) -> None:
        """Record a turn the per-agent cap refused while the pool still had money.

        Fires only when the cap is what said no: this agent has spent its whole
        allowance and the team has not spent the pool. Without the record the two
        ways an agent can stop contributing look identical afterwards — "the
        model never used this role" and "the model used this role until its
        allowance ran out" both end as an agent that takes no further turns, and
        they are opposite findings about the same run.

        ``requested`` is what the shared pool alone would have lent this turn,
        which is the amount the cap withheld. ``remaining`` is the agent's own
        allowance left, unclamped, so an agent that overshot its cap inside a
        single turn reads as the negative it is, and ``would_exceed_by`` is
        measured against it.

        Observational: a record that cannot be built must not overturn the
        allocation it describes.
        """
        tracer = self._tracer
        if tracer is None:
            return
        try:
            scb = self.table.get(aid)
            state = getattr(scb, "state", None)
            payload = {
                "agent_id": aid,
                "role": scb.agent.name if scb is not None else None,
                "step_count": getattr(state, "step_count", None),
                "requested": max(0, pool_remaining),
                "per_agent_cap": cap,
                "used": used,
                "remaining": cap - used,
                "pool_remaining": pool_remaining,
                "spent": self.used_tokens,
                "total": self._max_budget_tokens,
                "team_size": self._declared_team_size(),
                "share": PER_AGENT_BUDGET_SHARE,
                "would_exceed_by": max(0, pool_remaining) - (cap - used),
            }
            tracer.log_step(step_type="agent_cap_reached", payload=payload)
        except Exception as exc:  # noqa: BLE001 — observability is non-authoritative
            logger.error("agent budget cap trace failed: %s", exc)

    def _reserve_turn_lease(self, aid: int) -> int:
        """Lease this turn's tokens to a resuming session.

        Declared roster: the pool's unspent remainder, bounded by the agent's
        own ``per_agent_cap``. Dynamic roster: every token not already reserved
        by someone else's live lease.
        """
        self._release_turn_lease(aid)
        baseline = self._session_used_tokens(aid)
        cap = self._per_agent_cap()
        if cap is None:
            grant = max(0, self._max_budget_tokens - self._budget_committed())
        else:
            grant = self._grant_under_cap(aid, cap, baseline)
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
        if self._per_agent_cap() is not None:
            # Declared roster: one path for every agent, agent 0 included. A zero
            # grant here means this agent has spent its own cap (or the pool is
            # dry), so let the turn start and reach an explicit budget terminal
            # rather than leave a durable inbox entry that can never become
            # runnable.
            self._reserve_turn_lease(aid)
            return True
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
