"""Compatibility shim; event contracts now live in opencollab.domain.events."""

from opencollab.domain.events import (
    DomainEvent,
    SessionEventType,
    SessionRuntimeEvent,
    TeamEvent,
    TeamEventType,
)

__all__ = [
    "DomainEvent",
    "SessionEventType",
    "SessionRuntimeEvent",
    "TeamEvent",
    "TeamEventType",
]
