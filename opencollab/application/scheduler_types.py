"""Pure data types used by the scheduler.

Kept separate from ``scheduler.py`` so the dataclasses can be imported without
pulling in the full ``Scheduler`` runtime. ``LaunchSpec`` in particular is part
of the launch-lifecycle contract referenced by ``application.session`` and the
bootstrap factories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from opencollab.domain.session import SessionPhase


class SchedulerTurnError(RuntimeError):
    """A targeted public turn reached a non-success terminal session phase."""

    def __init__(
        self,
        aid: int,
        phase: SessionPhase,
        terminal_reason: str | None,
        partial_answer: str | None,
    ) -> None:
        self.aid = aid
        self.phase = phase
        self.terminal_reason = terminal_reason
        self.partial_answer = partial_answer
        reason = terminal_reason or phase.value
        super().__init__(f"Agent aid {aid} {phase.value}: {reason}")


class SchedulerStalledError(RuntimeError):
    """No scheduler-owned task can advance a non-terminal session."""

    def __init__(self, aid: int) -> None:
        self.aid = aid
        super().__init__(f"Scheduler stalled for aid {aid}: no active producer can advance it")


class RosterEntry(TypedDict):
    """One row of a team roster (``Scheduler.team_snapshot``/``team_roster``).

    ``aid`` is ``None`` for a configured-but-unspawned role. ``phase`` is a
    ``SessionPhase`` value for a live agent, or the synthetic ``"available"``
    for an unspawned role; there is no ``"completed"`` phase.
    """

    aid: int | None
    role: str
    parent_aid: int | None
    phase: str
    busy: bool


# Live phases that present as "settled" (idle) in a roster display.
_SETTLED_PHASES = frozenset({"done"})


def roster_display_state(entry: RosterEntry) -> str:
    """Map a roster entry (phase/busy) to a single display-state label.

    Shared by the prompt toolbar and the TUI team panel so their state
    vocabulary cannot drift: a busy agent is ``"running"``; a settled phase is
    ``"idle"``; otherwise the raw phase is shown.
    """
    if entry.get("busy"):
        return "running"
    phase = entry.get("phase", "?")
    return "idle" if phase in _SETTLED_PHASES else phase


@dataclass(frozen=True)
class LaunchSpec:
    """Launch-time persistence spec for a session process.

    Carries *where* to restore from and *where* to checkpoint. The scheduler
    sequences resume/seed as a launch lifecycle step (``create_init_process``
    -> ``Session.apply_launch``); the Session/Store own *how*. Pure data — the
    scheduler forwards it without interpreting the contents.
    """

    session_file: str | None = None
    auto_save_path: str | None = None


@dataclass(frozen=True)
class QueuedTeammateMessage:
    from_aid: int
    to_aid: int
    summary: str
    content: str
    xml: str
    sent_at: str = ""
    message_id: str = ""


__all__ = [
    "LaunchSpec",
    "QueuedTeammateMessage",
    "RosterEntry",
    "SchedulerStalledError",
    "SchedulerTurnError",
    "roster_display_state",
]
