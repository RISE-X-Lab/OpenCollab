"""Pure domain for team topology — no I/O, no asyncio.

A ``Topology`` is the directed graph of which role may delegate to (spawn) or
message which other role. It is the policy the Scheduler consults before
creating a child agent or routing an inter-agent message.

``allow_all`` is the permissive default used when no team file is configured:
it preserves the legacy 1-level star where the lead can spawn any ad-hoc role.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Topology:
    """Directed who-may-talk-to-whom graph over role names.

    ``edges`` maps a source role to the set of roles it may spawn/message.
    When ``allow_all`` is set, every edge is permitted regardless of ``edges``.
    """

    edges: dict[str, frozenset[str]] = field(default_factory=dict)
    allow_all: bool = False

    def allows(self, src: str, dst: str) -> bool:
        if self.allow_all:
            return True
        return dst in self.edges.get(src, frozenset())


__all__ = ["Topology"]
