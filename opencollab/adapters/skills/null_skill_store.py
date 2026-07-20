"""The default skill store: knows no skills.

``NullSkillStore`` is wired by default so the system is byte-identical to today
when no ``skills/`` directory exists — no catalog appears, no body resolves.
"""

from __future__ import annotations

from opencollab.domain.skill import SkillManifest


class NullSkillStore:
    """A ``SkillStorePort`` that holds no skills."""

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return ()

    def get_body(self, name: str) -> str | None:
        return None


__all__ = ["NullSkillStore"]
