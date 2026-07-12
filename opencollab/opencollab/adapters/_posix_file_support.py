"""Explicit POSIX boundary for descriptor-safe local file operations."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import ModuleType

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through the capability seam
    _fcntl = None


class UnsupportedSafeFilePlatformError(RuntimeError):
    """The host cannot provide the primitives required for safe local I/O."""


_REQUIRED_DIR_FD_FUNCTION_NAMES = ("link", "mkdir", "open", "rename", "stat")
_REQUIRED_FOLLOW_SYMLINK_FUNCTION_NAMES = ("link", "stat")
_SUPPORTED_DIR_FD_FUNCTION_NAMES = frozenset(
    getattr(function, "__name__", "")
    for function in getattr(os, "supports_dir_fd", set())
)
_SUPPORTED_FOLLOW_SYMLINK_FUNCTION_NAMES = frozenset(
    getattr(function, "__name__", "")
    for function in getattr(os, "supports_follow_symlinks", set())
)
_SUPPORTS_FD_LISTDIR = "listdir" in {
    getattr(function, "__name__", "")
    for function in getattr(os, "supports_fd", set())
}
_MACOS_ROOT_ALIASES = {
    "/etc": "/private/etc",
    "/tmp": "/private/tmp",
    "/var": "/private/var",
}


def require_posix_file_support() -> ModuleType:
    """Return ``fcntl`` after checking every security-critical capability."""
    missing: list[str] = []
    fcntl_module = _fcntl
    if os.name != "posix":
        missing.append("POSIX host")
    if fcntl_module is None or not callable(getattr(fcntl_module, "flock", None)):
        missing.append("fcntl.flock")
    if not hasattr(os, "O_NOFOLLOW"):
        missing.append("O_NOFOLLOW")
    if not set(_REQUIRED_DIR_FD_FUNCTION_NAMES).issubset(
        _SUPPORTED_DIR_FD_FUNCTION_NAMES
    ):
        missing.append("dir_fd operations")
    if not set(_REQUIRED_FOLLOW_SYMLINK_FUNCTION_NAMES).issubset(
        _SUPPORTED_FOLLOW_SYMLINK_FUNCTION_NAMES
    ):
        missing.append("no-follow link/stat operations")
    if not _SUPPORTS_FD_LISTDIR:
        missing.append("file-descriptor listdir")
    # CPython exposes src_dir_fd/dst_dir_fd on os.replace but registers only
    # the underlying os.rename implementation in os.supports_dir_fd.
    if missing:
        capabilities = ", ".join(missing)
        raise UnsupportedSafeFilePlatformError(
            "descriptor-safe local file operations require POSIX support "
            f"({capabilities} unavailable)"
        )
    if fcntl_module is None:  # Static narrowing after the aggregated error.
        raise UnsupportedSafeFilePlatformError(
            "descriptor-safe local file operations require POSIX support"
        )
    return fcntl_module


def _verified_macos_root_alias(alias: str, canonical: str) -> bool:
    """Validate one immutable, root-owned macOS compatibility alias."""
    try:
        alias_entry = os.lstat(alias)
        canonical_entry = os.lstat(canonical)
        followed_alias = os.stat(alias)
        link_target = os.readlink(alias)
    except OSError:
        return False
    allowed_targets = {canonical, canonical.removeprefix(os.sep)}
    return (
        stat.S_ISLNK(alias_entry.st_mode)
        and alias_entry.st_uid == 0
        and link_target in allowed_targets
        and stat.S_ISDIR(canonical_entry.st_mode)
        and canonical_entry.st_uid == 0
        and (followed_alias.st_dev, followed_alias.st_ino)
        == (canonical_entry.st_dev, canonical_entry.st_ino)
    )


def normalize_trusted_root_alias(path: Path) -> Path:
    """Expand only verified macOS ``/etc``, ``/var``, and ``/tmp`` aliases.

    Deliberately avoid ``realpath``: descendants remain subject to component-by-
    component no-follow traversal by the caller.
    """
    absolute = Path(os.path.abspath(path))
    if sys.platform != "darwin":
        return absolute
    parts = absolute.parts
    if len(parts) < 2:
        return absolute
    alias = f"{os.sep}{parts[1]}"
    canonical = _MACOS_ROOT_ALIASES.get(alias)
    if canonical is None or not _verified_macos_root_alias(alias, canonical):
        return absolute
    return Path(canonical).joinpath(*parts[2:])


__all__ = [
    "UnsupportedSafeFilePlatformError",
    "normalize_trusted_root_alias",
    "require_posix_file_support",
]
