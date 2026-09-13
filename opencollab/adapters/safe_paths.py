"""Canonical path handling shared by safe filesystem adapters."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MACOS_SYSTEM_ALIASES = (
    (Path("/tmp"), Path("/private/tmp")),
    (Path("/var"), Path("/private/var")),
)


def _canonicalize_system_alias(path: Path) -> Path:
    if sys.platform != "darwin":
        return path
    for alias, canonical in _MACOS_SYSTEM_ALIASES:
        try:
            relative = path.relative_to(alias)
        except ValueError:
            continue
        if Path(os.path.realpath(alias)) == canonical and canonical.is_dir():
            return canonical / relative
    return path


def canonicalize_system_path(path: str | os.PathLike[str]) -> Path:
    """Return one absolute path with stable macOS system-directory aliases."""
    value = os.fspath(path)
    if not value or "\0" in value:
        raise ValueError("path must be non-empty text without NUL bytes")
    return _canonicalize_system_alias(Path(os.path.abspath(value)))
