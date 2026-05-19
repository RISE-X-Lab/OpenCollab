"""Compatibility shim — runtime fan-out moved to application.event_bus.

This module remains because:
- ``core/events.py`` re-exports from here, and characterization tests pin
  ``from opencollab.core.events import EventBus, SessionEvent`` identities.
- Existing tests still import EventBus / SessionEvent from this location.

New production code should import EventBus from
``opencollab.application.event_bus`` and event value types from
``opencollab.domain.events``.
"""

from opencollab.application.event_bus import EventBus, EventCallback, EventSink
from opencollab.domain.events import SessionRuntimeEvent, TeamEvent

# Legacy alias preserved for the v3 event split migration.
SessionEvent = SessionRuntimeEvent

__all__ = [
    "EventBus",
    "EventCallback",
    "EventSink",
    "SessionEvent",
    "SessionRuntimeEvent",
    "TeamEvent",
]
