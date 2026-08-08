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
import os
import stat
from pathlib import Path

import yaml

from opencollab.adapters.safe_files import read_regular_text
from opencollab.adapters.tools._output import truncate
from opencollab.domain.skill import SkillManifest

logger = logging.getLogger(__name__)

# Single cap site for skills. A skill body is either available in full or is
# skipped: returning a silently truncated instruction set would violate the
# ``use_skill`` tool contract.
SKILL_BODY_MAX_CHARS = 8000
SKILL_DESCRIPTION_MAX_CHARS = 500
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_PACKAGES = 256
MAX_SKILL_ROOT_ENTRIES = 4_096

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

    frontmatter = yaml.safe_load("\n".join(lines[1:close_idx]))
    if not isinstance(frontmatter, dict):
        return None

    name = frontmatter.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return None
    body = "\n".join(lines[close_idx + 1 :]).strip()
    return name, description, body


class FileSkillStore:
    """A ``SkillStorePort`` backed by ``<root>/<name>/SKILL.md`` packages."""

    def __init__(self, root: Path) -> None:
        self._manifests: list[SkillManifest] = []
        self._bodies: dict[str, str] = {}
        self._load_diagnostics: list[str] = []
        self._load(Path(root))

    def _load(self, root: Path) -> None:
        try:
            inspected = root.lstat()
        except OSError:
            return
        if not stat.S_ISDIR(inspected.st_mode):
            return
        skill_dirs: list[Path] = []
        with os.scandir(root) as entries:
            scanned = 0
            for entry in entries:
                scanned += 1
                if scanned > MAX_SKILL_ROOT_ENTRIES:
                    raise ValueError(
                        "skills root entries exceed limit of "
                        f"{MAX_SKILL_ROOT_ENTRIES}"
                    )
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_directory:
                    continue
                skill_dirs.append(Path(entry.path))
                if len(skill_dirs) > MAX_SKILL_PACKAGES:
                    raise ValueError(
                        f"skill packages exceed limit of {MAX_SKILL_PACKAGES}"
                    )
        for skill_dir in sorted(skill_dirs):
            self._load_one(skill_dir / "SKILL.md")

    def _load_one(self, skill_md: Path) -> None:
        try:
            text = read_regular_text(
                skill_md,
                max_bytes=MAX_SKILL_FILE_BYTES,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
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
        if len(body) > SKILL_BODY_MAX_CHARS:
            diagnostic = (
                f"skill {name!r} rejected: body exceeds "
                f"{SKILL_BODY_MAX_CHARS} characters"
            )
            self._load_diagnostics.append(diagnostic)
            logger.warning("%s (%s)", diagnostic, skill_md)
            return
        if name in self._bodies:
            logger.debug("skipping duplicate skill name %r at %s", name, skill_md)
            return
        self._manifests.append(
            SkillManifest(
                name=name,
                description=truncate(description, SKILL_DESCRIPTION_MAX_CHARS),
            )
        )
        self._bodies[name] = body

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(self._manifests)

    def get_body(self, name: str) -> str | None:
        return self._bodies.get(name)

    @property
    def load_diagnostics(self) -> tuple[str, ...]:
        """Explicit reasons that otherwise valid skill packages were rejected."""
        return tuple(self._load_diagnostics)


__all__ = ["FileSkillStore", "SKILL_BODY_MAX_CHARS", "SKILL_DESCRIPTION_MAX_CHARS"]
