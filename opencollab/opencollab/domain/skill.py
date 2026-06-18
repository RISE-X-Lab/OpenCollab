"""Pure domain value object for a skill's catalog metadata.

A *skill* is a packaged unit of INSTRUCTIONS (name + description + body) loaded
into an agent's context when relevant — not a tool/function. The body is fetched
on invocation, so the catalog metadata the model reads to decide whether to
invoke is just name + description; that is what ``SkillManifest`` carries.

This module is policy only — no I/O, no outer-layer imports — so it sits at the
center of the dependency graph alongside the other ``domain`` modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillManifest:
    """A skill's catalog metadata — what the model sees to decide whether to invoke.

    The body is intentionally absent: the catalog only needs ``name`` +
    ``description``; the full instruction body is retrieved separately on
    invocation (see ``application.ports.SkillStorePort.get_body``).
    """

    name: str          # unique invocation key, e.g. "debug-flaky-tests"
    description: str    # one-liner shown in the catalog


__all__ = ["SkillManifest"]
