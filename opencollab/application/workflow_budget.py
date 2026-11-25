"""Shared token-budget bookkeeping for deterministic workflows."""

from __future__ import annotations

import asyncio
import logging
import operator
import os
from dataclasses import dataclass
from typing import Any

from opencollab.application.workflow_collections import WorkflowBudgetExceeded, _CollectionBudget

logger = logging.getLogger(__name__)

# Preserve the finite one-shot escape for callers that configured a finite pool.
UNBOUNDED_SESSION_BUDGET = 1_000_000

def _unbounded_limits_enabled() -> bool:
    return os.environ.get("OPENCOLLAB_UNBOUNDED_LIMITS", "").strip().lower() in {
        "1",
        "true",
    }
def _positive_concurrency(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed

def _positive_budget(value: object, name: str = "budget") -> int | None:
    """Normalize an optional per-call budget to a strictly positive integer."""
    if value is None:
        return None
    return _positive_concurrency(value, name)

class WorkflowBudget:
    """Read-only view over token spend across all sessions created so far."""

    def __init__(self, total: int | None, sessions: list[Any]) -> None:
        self._total = total
        self._sessions = sessions
        self._leases: list[_BudgetLease] = []
        self._parent_allocation: tuple[WorkflowBudget, _BudgetLease] | None = None

    @property
    def total(self) -> int | None:
        if self._total is not None and self._parent_allocation is not None:
            return self._parent_allocation[1].total
        return self._total

    def spent(self) -> int:
        return sum(int(getattr(s, "used_tokens", 0)) for s in self._sessions)

    def _track_session(self, session: Any) -> None:
        self._sessions.append(session)
        if self._parent_allocation is not None:
            parent, allocation = self._parent_allocation
            allocation.sessions.append(session)
            parent._track_session(session)

    def remaining(self) -> float:
        total = self.total
        if total is None:
            return float("inf")
        spent = self.spent()
        if self._parent_allocation is not None:
            parent, allocation = self._parent_allocation
            # A sibling can overspend while this workflow waits for an agent slot.
            total = min(total, spent + parent.remaining() + allocation.remaining())
        reserved_unspent = sum(lease.remaining() for lease in self._leases)
        return total - spent - reserved_unspent

    def reserve(self, lease: _BudgetLease) -> None:
        self._leases.append(lease)

    def release(self, lease: _BudgetLease) -> None:
        try:
            self._leases.remove(lease)
        except ValueError:
            pass

    def _parent_recovery_room(self) -> int:
        if self._parent_allocation is None:
            return 0
        parent, allocation = self._parent_allocation
        return parent._recovery_room(allocation)

    def _collection_recovery_share(self, collection: _CollectionBudget, free: int) -> int:
        # Reservations held by this collection already count toward its share.
        reserved = sum(
            lease.remaining() for lease in self._leases if collection.includes(lease.collection)
        )
        balance = max(collection.entry_balance, free + int(reserved))
        if collection.parent is not None:
            balance = min(balance, self._collection_recovery_share(collection.parent, free))
        collection.recovered_share = max(collection.recovered_share, balance // collection.size)
        return collection.recovered_share

    def _recovery_room(self, lease: _BudgetLease) -> int:
        if lease.total is None or lease.recovery_limit is None or lease not in self._leases:
            return 0
        free = int(self.remaining()) + self._parent_recovery_room()
        if free <= 0:
            return 0
        limit = lease.recovery_limit
        if isinstance(limit, _CollectionBudget):
            limit = self._collection_recovery_share(limit, free)
        return max(0, min(limit - lease.total, free))

    def _claim_parent_capacity(self, tokens: int) -> None:
        if self._parent_allocation is None:
            return
        missing = tokens - int(self.remaining())
        if missing > 0:
            parent, allocation = self._parent_allocation
            parent._extend_lease(allocation, missing)

    def _extend_lease(self, lease: _BudgetLease, tokens: int) -> None:
        self._claim_parent_capacity(tokens)
        assert lease.total is not None
        lease.total += tokens
        lease.reserved = lease.total

    def _claim_recovered_budget(self, lease: _BudgetLease, minimum: int, preferred: int) -> int | None:
        growth = min(preferred, self._recovery_room(lease))
        if growth < minimum:
            return None
        self._extend_lease(lease, growth)
        return lease.total

@dataclass
class _BudgetEscapeState:
    """One atomic forced-write claim shared by a workflow and its children."""

    lock: asyncio.Lock
    used: bool = False

@dataclass(eq=False)
class _BudgetLease:
    """A per-call allocation whose identity remains distinct from equal grants."""

    total: int | None
    reserved: int
    sessions: list[Any]
    pending_tasks: list[asyncio.Task[Any]] | None = None
    recovery_limit: int | _CollectionBudget | None = None
    collection: _CollectionBudget | None = None

    def remaining(self) -> float:
        if self.total is None:
            return float("inf")
        spent = sum(max(0, int(getattr(s, "used_tokens", 0))) for s in self.sessions)
        return max(0, self.total - spent)

@dataclass
class _ConcurrencyPermit:
    """One semaphore slot owned by a task and reusable by nested agent calls."""

    owner: asyncio.Task[Any] | None
    pending_cleanup_tasks: list[asyncio.Task[Any]]

class WorkflowBudgetMixin:
    """Allocate call budgets while retaining workflow accounting and trace evidence."""

    async def _refresh_budget_lease(
        self,
        lease: _BudgetLease,
        *,
        cap: int | None,
        over_budget_ok: bool,
        label: str | None,
    ) -> _BudgetLease:
        if lease.reserved == 0:
            return lease
        # Keep other reservations intact when an earlier request exceeds its grant.
        available = max(0, int(self.budget.remaining() + lease.remaining()))
        if available <= 0:
            self.budget.release(lease)
            return await self._acquire_budget_lease(
                cap, over_budget_ok=over_budget_ok, label=label,
            )
        lease.total = min(lease.total, available)
        lease.reserved = lease.total
        return lease

    def _session_budget(self) -> int | None:
        lease = self._active_budget_lease.get()
        if lease is not None:
            remaining = lease.remaining()
            return None if remaining == float("inf") else int(remaining)
        remaining = self.budget.remaining()
        if remaining == float("inf"):
            return None
        # A concurrent spend can make remaining negative after agent() checks it.
        # Floor the per-session budget at zero.
        return max(0, int(remaining))

    def _capped_session_budget(self, cap: int | None) -> int | None:
        """Session budget = the live global remaining, optionally lowered to a
        caller-supplied per-call ``cap``. ``min`` keeps a per-call allocation
        from overshooting the shared pool while the cap bounds a single runaway
        session; ``None`` reproduces the prior whole-pool behaviour."""
        base = self._session_budget()
        if getattr(self, "_limit_mode", "environment") == "environment" and _unbounded_limits_enabled():
            return None
        if base is None:
            return max(0, cap) if cap is not None else None
        return min(max(0, cap), base) if cap is not None else base

    def _trace_budget_decision(
        self,
        step_type: str,
        *,
        cap: int | None,
        remaining: float,
        label: str | None,
        over_budget_ok: bool = False,
    ) -> None:
        """Record shared-pool refusal or the ``over_budget_ok`` exception.

        ``seq`` counts sessions created before this decision. ``agent_id`` is None
        because no session exists yet, while ``label`` identifies the caller and
        joins its transcript. Keep these fields separate from session integer aids.

        ``remaining`` retains the live, unclamped balance. ``would_exceed_by`` is
        ``requested_cap - remaining``, or None for an uncapped request. Trace failures
        are logged without changing the allocation decision.
        """
        try:
            payload: dict[str, Any] = {
                "seq": len(self._sessions),
                # Before agent creation, keep the caller's label separate from the empty agent id.
                "agent_id": None,
                "label": str(label)[:240] if label else None,
                "requested_cap": cap,
                "remaining": int(remaining),
                "spent": self.budget.spent(),
                "total": self.budget.total,
                "would_exceed_by": (
                    None if cap is None else max(0, cap) - int(remaining)
                ),
            }
            if over_budget_ok:
                payload["over_budget_ok"] = True
        except Exception as exc:  # noqa: BLE001 — observability is non-authoritative
            logger.error("workflow %s trace failed: %s", step_type, exc)
            return
        self._trace_step(step_type, payload)

    async def _acquire_budget_lease(
        self,
        cap: int | None,
        *,
        over_budget_ok: bool,
        label: str | None = None,
    ) -> _BudgetLease:
        """Atomically reserve one agent call's maximum token allocation.

        ``label`` is carried for the trace record only — it names the caller in
        ``budget_refusal`` / ``budget_escape`` and changes no allocation.
        """
        cap = _positive_budget(cap)
        if getattr(self, "_limit_mode", "environment") == "environment" and _unbounded_limits_enabled():
            return _BudgetLease(total=None, reserved=0, sessions=[])
        self._budget_waiters += 1
        try:
            # Let sibling tasks launched by one gather register as contenders
            # before the first uncapped caller chooses its share.
            await asyncio.sleep(0)
            async with self._budget_lock:
                remaining = self.budget.remaining()
                if remaining == float("inf"):
                    total = max(0, cap) if cap is not None else None
                    return _BudgetLease(total=total, reserved=0, sessions=[])

                available = max(0, int(remaining))
                recoverable = max(0, int(remaining) + self.budget._parent_recovery_room())
                initial = available or min(1, recoverable)
                if initial <= 0 and not over_budget_ok:
                    self._trace_budget_decision(
                        "budget_refusal",
                        cap=cap,
                        remaining=remaining,
                        label=label,
                    )
                    raise WorkflowBudgetExceeded(
                        f"workflow budget exhausted: spent {self.budget.spent()} "
                        f"of {self.budget.total}"
                    )
                if over_budget_ok and initial <= 0:
                    async with self._budget_escape_state.lock:
                        if self._budget_escape_state.used:
                            self._trace_budget_decision(
                                "budget_refusal",
                                cap=cap,
                                remaining=remaining,
                                label=label,
                                over_budget_ok=True,
                            )
                            raise WorkflowBudgetExceeded(
                                f"workflow budget exhausted: spent {self.budget.spent()} "
                                f"of {self.budget.total}; the one over-budget escape "
                                "has already been used"
                            )
                        # Claim before tracing/building so failures cannot turn the
                        # one-shot escape into a retry loop.
                        self._budget_escape_state.used = True
                    self._trace_budget_decision(
                        "budget_escape",
                        cap=cap,
                        remaining=remaining,
                        label=label,
                        over_budget_ok=True,
                    )
                    total = max(0, cap) if cap is not None else UNBOUNDED_SESSION_BUDGET
                    return _BudgetLease(total=total, reserved=0, sessions=[])

                collection = self._active_collection_budget.get()
                recovery_limit = None
                if cap is not None:
                    total = min(cap, initial)
                    if self.budget._parent_allocation is not None:
                        recovery_limit = cap
                elif collection is not None:
                    total = min(collection.entry_share, initial)
                    recovery_limit = collection
                else:
                    waiters = max(1, self._budget_waiters)
                    total = max(1, initial // waiters)
                    ceiling = recoverable // waiters
                    if ceiling > total:
                        recovery_limit = ceiling
                lease = _BudgetLease(
                    total=total, reserved=total, sessions=[], recovery_limit=recovery_limit, collection=collection,
                )
                if total > available:
                    self.budget._claim_parent_capacity(total)
                self.budget.reserve(lease)
                return lease
        finally:
            self._budget_waiters -= 1
