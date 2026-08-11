"""Shared token-budget bookkeeping for deterministic workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


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

    total: int
    reserved: int
    sessions: list[Any]
    pending_tasks: list[asyncio.Task[Any]] | None = None

    def remaining(self) -> int:
        spent = sum(max(0, int(getattr(s, "used_tokens", 0))) for s in self.sessions)
        return max(0, self.total - spent)
