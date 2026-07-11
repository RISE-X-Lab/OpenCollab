"""Host-owned authentication material for team retirement records."""

from __future__ import annotations

import re
import secrets
import stat
from pathlib import Path

from opencollab.adapters.retirement_registry import RETIREMENT_SIGNING_KEY_BYTES
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    unlink_regular_file_durable,
)


def _identity(path: Path, *, expected_size: int, label: str) -> tuple[int, int]:
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or current.st_size != expected_size:
        raise OSError(f"{label} is not a bounded regular file")
    return current.st_dev, current.st_ino


def create_retirement_log(path: Path) -> tuple[int, int]:
    create_regular_bytes_atomic(path, b"", max_bytes=0, mode=0o600)
    return _identity(path, expected_size=0, label="internal retirement log")


def create_retirement_key(path: Path) -> tuple[int, int]:
    """Create one key for compatibility with older callers."""
    create_regular_bytes_atomic(
        path,
        secrets.token_bytes(RETIREMENT_SIGNING_KEY_BYTES),
        max_bytes=RETIREMENT_SIGNING_KEY_BYTES,
        mode=0o600,
    )
    return _identity(
        path,
        expected_size=RETIREMENT_SIGNING_KEY_BYTES,
        label="internal retirement key",
    )


def create_retirement_keys(signing_path: Path, verification_path: Path) -> str:
    """Create two separately owned files containing one fresh random key."""
    key = secrets.token_bytes(RETIREMENT_SIGNING_KEY_BYTES)
    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        for path in (signing_path, verification_path):
            create_regular_bytes_atomic(
                path,
                key,
                max_bytes=RETIREMENT_SIGNING_KEY_BYTES,
                mode=0o600,
            )
            identity = _identity(
                path,
                expected_size=RETIREMENT_SIGNING_KEY_BYTES,
                label="internal retirement key",
            )
            created.append((path, identity))
    except BaseException:
        for path, identity in reversed(created):
            unlink_regular_file_durable(path, expected_target_identity=identity)
        raise
    return "|".join(f"{dev}:{ino}" for _path, (dev, ino) in created)


def remove_retirement_file(path: Path, expected_identity: str) -> None:
    match = re.fullmatch(r"([0-9]+):([0-9]+)", expected_identity)
    if match is None:
        raise ValueError("internal retirement file identity is invalid")
    removed = unlink_regular_file_durable(
        path,
        expected_target_identity=(int(match.group(1)), int(match.group(2))),
    )
    if not removed:
        raise OSError("internal retirement file disappeared before retirement")


remove_retirement_log = remove_retirement_file


__all__ = [
    "create_retirement_key",
    "create_retirement_keys",
    "create_retirement_log",
    "remove_retirement_file",
    "remove_retirement_log",
]
