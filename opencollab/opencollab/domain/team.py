"""Pure domain for team topology — no I/O, no asyncio.

A ``Topology`` is the directed graph of which role may delegate to (spawn) or
message which other role. It is the policy the Scheduler consults before
creating a child agent or routing an inter-agent message.

``allow_all`` is the permissive default used when no team file is configured:
it preserves the legacy 1-level star where the lead can spawn any ad-hoc role.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.domain.identity import role_collision_key, validate_role_identity


@dataclass(frozen=True)
class Topology:
    """Directed who-may-talk-to-whom graph over role names.

    ``edges`` maps a source role to the set of roles it may spawn/message.
    When ``allow_all`` is set, every edge is permitted regardless of ``edges``.
    """

    edges: dict[str, frozenset[str]] = field(default_factory=dict)
    allow_all: bool = False

    def __post_init__(self) -> None:
        normalized: dict[str, frozenset[str]] = {}
        source_keys: dict[str, str] = {}
        for raw_source, raw_destinations in self.edges.items():
            source = validate_role_identity(raw_source)
            source_key = role_collision_key(source)
            if source_key in source_keys:
                raise ValueError(
                    "topology source identities collide after normalization: "
                    f"{source_keys[source_key]!r} and {raw_source!r}"
                )
            source_keys[source_key] = source
            destinations: list[str] = []
            destination_keys: set[str] = set()
            for raw_destination in raw_destinations:
                destination = validate_role_identity(raw_destination)
                destination_key = role_collision_key(destination)
                if destination_key in destination_keys:
                    raise ValueError(
                        "topology destination identities collide after normalization "
                        f"for source {raw_source!r}"
                    )
                destination_keys.add(destination_key)
                destinations.append(destination)
            normalized[source] = frozenset(destinations)
        object.__setattr__(self, "edges", normalized)

    def allows(self, src: str, dst: str) -> bool:
        src = validate_role_identity(src)
        dst = validate_role_identity(dst)
        if self.allow_all:
            return True
        source_key = role_collision_key(src)
        destination_key = role_collision_key(dst)
        for source, destinations in self.edges.items():
            if role_collision_key(source) != source_key:
                continue
            return any(
                role_collision_key(destination) == destination_key
                for destination in destinations
            )
        return False


__all__ = ["Topology"]
