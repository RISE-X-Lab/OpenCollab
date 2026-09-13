"""Shared token-budget bookkeeping for deterministic workflows."""

from __future__ import annotations

import asyncio
import logging
import operator
import os
from dataclasses import dataclass
from typing import Any

from opencollab.application.workflow_collections import WorkflowBudgetExceeded

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

    @property
    def total(self) -> int | None:
        return self._total

    def spent(self) -> int:
        return sum(int(getattr(s, "used_tokens", 0)) for s in self._sessions)

    def remaining(self) -> float:
        if self._total is None:
            return float("inf")
        reserved_unspent = sum(lease.remaining() for lease in self._leases)
        return self._total - self.spent() - reserved_unspent

    def reserve(self, lease: _BudgetLease) -> None:
        self._leases.append(lease)

    def release(self, lease: _BudgetLease) -> None:
        try:
            self._leases.remove(lease)
        except ValueError:
            pass


@dataclass
class _BudgetLease:
    """A per-call token allocation held while one workflow agent is active."""

    total: int | None
    reserved: int
    sessions: list[Any]
    pending_tasks: list[asyncio.Task[Any]] | None = None

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

    def _session_budget(self) -> int | None:
        lease = self._active_budget_lease.get()
        if lease is not None:
            remaining = lease.remaining()
            return None if remaining == float("inf") else int(remaining)
        remaining = self.budget.remaining()
        if remaining == float("inf"):
            return None
        # Clamp to zero: a concurrent agent's spend can land between agent()'s
        # budget gate and this call, driving ``remaining`` negative. A negative
        # per-session budget is nonsensical, so floor it at 0.
        return max(0, int(remaining))

    def _capped_session_budget(self, cap: int | None) -> int | None:
        """Session budget = the live global remaining, optionally lowered to a
        caller-supplied per-call ``cap``. ``min`` keeps a per-call allocation
        from overshooting the shared pool while the cap bounds a single runaway
        session; ``None`` reproduces the prior whole-pool behaviour."""
        base = self._session_budget()
        if _unbounded_limits_enabled():
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
        """Record one shared-pool gate decision. Observation only.

        The pool's two decision points — the refusal that raises
        ``WorkflowBudgetExceeded`` and the ``over_budget_ok`` escape that waves a
        call through anyway — used to leave nothing behind: a finished run showed
        only ``reason="budget_exceeded"``, never which call was stopped, where in
        the run, or by how much, and the escape was invisible entirely.

        ``seq`` is how many agent sessions this context had already created when
        the decision was taken. The workflow layer keeps no step counter, and
        this is the only ordinal that says *where* in the run the call sat.

        ``agent_id`` is always ``None`` here, and that is the honest value: the
        lease is taken before any session is built, and every workflow session's
        agent is named ``workflow_agent`` regardless, so there is no agent to
        name. ``label`` carries the caller's own name instead — the same string
        that names that call's transcript file, hence the one key that joins to
        anything on disk. They are separate fields because a ``label`` sitting
        under an ``agent_id`` heading would join wrongly against the integer
        ``aid`` the session-level records carry, and silently.

        ``would_exceed_by`` measures the request against the live remaining
        balance (``requested_cap - remaining``), so a pool already in the hole
        makes it larger than the cap. ``None`` when no cap was named — an
        uncapped request has no amount, and a number there would be invented.
        ``remaining`` is that same live balance, unclamped, so an overdrawn pool
        reads as the negative it is.

        Guarded end to end: building this payload must never overturn the
        decision it describes, so a failure is logged and dropped.
        """
        try:
            payload: dict[str, Any] = {
                "seq": len(self._sessions),
                # Two separate slots on purpose. No agent exists yet, so the
                # agent id is honestly empty rather than filled with something
                # that merely looks like one; ``label`` is the caller's own
                # name, which is what the field actually holds.
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
        if _unbounded_limits_enabled():
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
                if available <= 0 and not over_budget_ok:
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
                if over_budget_ok and available <= 0:
                    if self._over_budget_escape_used:
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
                    self._over_budget_escape_used = True
                    self._trace_budget_decision(
                        "budget_escape",
                        cap=cap,
                        remaining=remaining,
                        label=label,
                        over_budget_ok=True,
                    )
                    total = max(0, cap) if cap is not None else UNBOUNDED_SESSION_BUDGET
                    return _BudgetLease(total=total, reserved=0, sessions=[])

                if cap is None:
                    collection_share = self._active_collection_budget.get()
                    if collection_share is not None:
                        total = min(collection_share, available)
                    else:
                        total = max(
                            1,
                            available // max(1, self._budget_waiters),
                        )
                else:
                    total = min(max(0, cap), available)
                lease = _BudgetLease(total=total, reserved=total, sessions=[])
                self.budget.reserve(lease)
                return lease
        finally:
            self._budget_waiters -= 1
