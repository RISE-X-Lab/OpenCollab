"""File-backed skill store.

Scans ``skills/<name>/SKILL.md`` once at construction. Each file carries
``---``-delimited frontmatter (``name:`` / ``description:``); everything after
the closing delimiter is the instruction body. The format aligns with Claude
Code's ``SKILL.md`` so a skill is a self-contained, droppable file package.

Robustness: a missing root directory yields an empty store, and a malformed or
garbage ``SKILL.md`` is skipped (logged at debug, never raised into the
constructor). The store is the SINGLE size-cap site (design decision #5): both
the catalog description and the body are capped here, so downstream consumers
trust the store and never re-cap.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opencollab.adapters.tools._output import truncate
from opencollab.domain.skill import SkillManifest

logger = logging.getLogger(__name__)

# Single cap site for skills. No global tool-guardrails char-cap constant exists
# on this branch (each tool defines its own, e.g. ``MAX_OUTPUT_CHARS``), so this
# is a local default in the spirit of those. The shared ``truncate`` helper
# (head+tail with a "… truncated …" marker) does the actual capping.
SKILL_BODY_MAX_CHARS = 8000
SKILL_DESCRIPTION_MAX_CHARS = 500

_FRONTMATTER_DELIMITER = "---"


def _parse_skill(text: str) -> tuple[str, str, str] | None:
    """Parse a SKILL.md into ``(name, description, body)`` or ``None`` if malformed.

    A well-formed file opens with a ``---`` line, has a frontmatter block
    containing at least a ``name:`` key, a closing ``---`` line, then the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None

    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIMITER:
            close_idx = idx
            break
    if close_idx is None:
        return None

    frontmatter: dict[str, str] = {}
    for raw in lines[1:close_idx]:
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        frontmatter[key.strip().lower()] = value.strip()

    name = frontmatter.get("name", "")
    if not name:
        return None
    description = frontmatter.get("description", "")
    body = "\n".join(lines[close_idx + 1 :]).strip()
    return name, description, body


class FileSkillStore:
    """A ``SkillStorePort`` backed by ``<root>/<name>/SKILL.md`` packages."""

    def __init__(self, root: Path) -> None:
        self._manifests: list[SkillManifest] = []
        self._bodies: dict[str, str] = {}
        self._load(Path(root))

    def _load(self, root: Path) -> None:
        if not root.is_dir():
            return
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            self._load_one(skill_dir / "SKILL.md")

    def _load_one(self, skill_md: Path) -> None:
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("skipping unreadable skill %s: %s", skill_md, exc)
            return
        try:
            parsed = _parse_skill(text)
        except Exception as exc:  # defensive: never raise into construction
            logger.debug("skipping malformed skill %s: %s", skill_md, exc)
            return
        if parsed is None:
            logger.debug("skipping malformed skill %s: bad frontmatter", skill_md)
            return
        name, description, body = parsed
        if name in self._bodies:
            logger.debug("skipping duplicate skill name %r at %s", name, skill_md)
            return
        self._manifests.append(
            SkillManifest(
                name=name,
                description=truncate(description, SKILL_DESCRIPTION_MAX_CHARS),
            )
        )
        self._bodies[name] = truncate(body, SKILL_BODY_MAX_CHARS)

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(self._manifests)

    def get_body(self, name: str) -> str | None:
        return self._bodies.get(name)


__all__ = ["FileSkillStore", "SKILL_BODY_MAX_CHARS", "SKILL_DESCRIPTION_MAX_CHARS"]
