"""Pure data types used by the scheduler.

Kept separate from ``scheduler.py`` so the dataclasses can be imported without
pulling in the full ``Scheduler`` runtime. ``LaunchSpec`` in particular is part
of the launch-lifecycle contract referenced by ``application.session`` and the
bootstrap factories.
"""

from __future__ import annotations

from dataclasses import dataclass


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


__all__ = ["LaunchSpec", "QueuedTeammateMessage"]
