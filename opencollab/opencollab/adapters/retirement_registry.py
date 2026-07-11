"""Trusted records for internal fail-closed filesystem retirements.

The retirement prefix is reserved by the harness.  Patch extraction may omit
only exact paths recorded here whose inode and mutation metadata still match.
Any other path in the reserved namespace remains visible to the extraction
guard and makes the extraction fail closed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from opencollab.adapters._posix_file_support import (
    normalize_trusted_root_alias,
    require_posix_file_support,
)
from opencollab.adapters._retirement_auth import ZERO_MAC, decode_records, encode_record

INTERNAL_RETIREMENT_LOG_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_LOG"
INTERNAL_RETIREMENT_KEY_FILE_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_KEY_FILE"
INTERNAL_RETIREMENT_WORKSPACE_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_WORKSPACE"
_INTERNAL_RETIREMENT_KEY_CONSUMED_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_KEY_CONSUMED"
RETIRED_FILE_PREFIX = ".opencollab-retired-"
MAX_RETIREMENT_LOG_BYTES = 512 * 1024
MAX_RETIREMENT_LOG_RECORDS = 1024
MAX_WORKSPACE_SCAN_ENTRIES = 200_000
RETIREMENT_SIGNING_KEY_BYTES = 32


@dataclass(frozen=True)
class _RetirementRecord:
    parent_dev: int
    parent_ino: int
    name: str
    file_dev: int
    file_ino: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    relative_path: str
    content_sha256: str

    @classmethod
    def capture(
        cls,
        parent_fd: int,
        name: str,
        *,
        relative_path: str = "",
    ) -> _RetirementRecord:
        if (
            not name.startswith(RETIRED_FILE_PREFIX)
            or os.sep in name
            or (os.altsep is not None and os.altsep in name)
        ):
            raise ValueError("retirement record name is outside the reserved namespace")
        parent = os.fstat(parent_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode) or not stat.S_ISREG(current.st_mode):
            raise OSError("retirement record must name a regular file in a real directory")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise OSError("retirement record changed while opening")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 65_536)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(fd)
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if cls._stable_identity(opened) != cls._stable_identity(after) or cls._stable_identity(
                after
            ) != cls._stable_identity(visible):
                raise OSError("retirement record changed while hashing")
        finally:
            os.close(fd)
        return cls(
            parent_dev=parent.st_dev,
            parent_ino=parent.st_ino,
            name=name,
            file_dev=current.st_dev,
            file_ino=current.st_ino,
            mode=stat.S_IFMT(after.st_mode),
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            nlink=after.st_nlink,
            relative_path=relative_path,
            content_sha256=digest.hexdigest(),
        )

    @staticmethod
    def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )

    @classmethod
    def from_payload(cls, payload: object) -> _RetirementRecord:
        if not isinstance(payload, dict):
            raise ValueError("retirement log record payload is invalid")
        values = {field: payload.get(field) for field in cls.__dataclass_fields__}
        numeric_fields = {
            "parent_dev",
            "parent_ino",
            "file_dev",
            "file_ino",
            "mode",
            "size",
            "mtime_ns",
            "ctime_ns",
            "nlink",
        }
        if any(
            isinstance(values[field], bool) or not isinstance(values[field], int)
            for field in numeric_fields
        ):
            raise ValueError("retirement log record has invalid numeric fields")
        name = values["name"]
        if (
            not isinstance(name, str)
            or not name.startswith(RETIRED_FILE_PREFIX)
            or os.sep in name
            or (os.altsep is not None and os.altsep in name)
        ):
            raise ValueError("retirement log record name is invalid")
        relative_path = values["relative_path"]
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\0" in relative_path
            or os.path.isabs(relative_path)
            or os.path.normpath(relative_path).startswith("..")
            or os.path.basename(relative_path) != name
        ):
            raise ValueError("retirement log record path is invalid")
        content_sha256 = values["content_sha256"]
        if (
            not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in content_sha256)
        ):
            raise ValueError("retirement log record digest is invalid")
        return cls(**values)

    def membership(self) -> tuple[int, int, str, int, int]:
        return (
            self.parent_dev,
            self.parent_ino,
            self.name,
            self.file_dev,
            self.file_ino,
        )

    def inode_identity(self) -> tuple[int, int]:
        return self.file_dev, self.file_ino

    def mutation_metadata(self) -> tuple[int, int, int, int, int]:
        return self.mode, self.size, self.mtime_ns, self.ctime_ns, self.nlink


@dataclass(frozen=True)
class RetirementSnapshot:
    """One authenticated tombstone identity used by extraction checkpoints."""

    relative_path: str
    parent_dev: int
    parent_ino: int
    file_dev: int
    file_ino: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int

    def to_payload(self) -> dict[str, object]:
        return dict(self.__dict__)


_records_lock = threading.Lock()
_records: list[_RetirementRecord] = []
_persistent_signing_key: bytes | None = None


def _append_persistent_record(
    path: str,
    record: _RetirementRecord,
    signing_key: bytes,
) -> None:
    fcntl = require_posix_file_support()
    parent_fd, name = _open_parent_no_symlinks(path)
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > MAX_RETIREMENT_LOG_BYTES
        ):
            raise OSError("internal retirement log is not a bounded regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            locked = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(locked.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (locked.st_dev, locked.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)
            ):
                raise OSError("internal retirement log identity changed")
            existing = _parse_persistent_records(_read_locked_log(fd), signing_key)
            if len(existing) >= MAX_RETIREMENT_LOG_RECORDS:
                raise OSError("internal retirement log exceeds its record limit")
            previous_mac = existing[-1][1] if existing else ZERO_MAC
            payload = encode_record(
                record.__dict__,
                signing_key,
                sequence=len(existing),
                previous_mac=previous_mac,
                expected_key_bytes=RETIREMENT_SIGNING_KEY_BYTES,
            )
            if locked.st_size + len(payload) > MAX_RETIREMENT_LOG_BYTES:
                raise OSError("internal retirement log exceeds its byte limit")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("internal retirement log append made no progress")
                view = view[written:]
            os.fsync(fd)
            appended = os.fstat(fd)
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (appended.st_dev, appended.st_ino) != (locked.st_dev, locked.st_ino)
                or (visible.st_dev, visible.st_ino) != (locked.st_dev, locked.st_ino)
                or appended.st_size != locked.st_size + len(payload)
                or visible.st_size != appended.st_size
            ):
                raise OSError("internal retirement log changed while appending")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def register_verified_retirement(parent_fd: int, retired_name: str) -> None:
    """Record one verified internal tombstone before patch extraction."""
    persistent_log = os.environ.get(INTERNAL_RETIREMENT_LOG_ENV, "")
    relative_path = _persistent_relative_path(parent_fd, retired_name) if persistent_log else ""
    record = _RetirementRecord.capture(
        parent_fd,
        retired_name,
        relative_path=relative_path,
    )
    if persistent_log:
        signing_key = _persistent_signing_key
        if signing_key is None:
            raise OSError("internal retirement signing key is unavailable")
        _append_persistent_record(persistent_log, record, signing_key)
    with _records_lock:
        _records.append(record)
        if len(_records) > MAX_RETIREMENT_LOG_RECORDS:
            del _records[: len(_records) - MAX_RETIREMENT_LOG_RECORDS]


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_parent_no_symlinks(path: str | os.PathLike[str]) -> tuple[int, str]:
    absolute = normalize_trusted_root_alias(os.path.abspath(path))
    parent, name = os.path.split(os.fspath(absolute))
    if not name or name in {".", ".."}:
        raise ValueError("internal retirement log path has no file component")
    components = os.path.normpath(parent).split(os.sep)[1:]
    fd = os.open(os.sep, _directory_flags())
    try:
        for component in components:
            if component in {"", ".", ".."}:
                raise OSError("internal retirement log has an unsafe parent")
            next_fd = os.open(component, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return result, name
    finally:
        if fd >= 0:
            os.close(fd)


def _consume_persistent_signing_key() -> bytes | None:
    key_path = os.environ.pop(INTERNAL_RETIREMENT_KEY_FILE_ENV, "")
    if not key_path:
        return None
    parent_fd, name = _open_parent_no_symlinks(key_path)
    fd = -1
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if os.environ.get(_INTERNAL_RETIREMENT_KEY_CONSUMED_ENV) == "1":
                return None
            raise
        if not stat.S_ISREG(before.st_mode) or before.st_size != RETIREMENT_SIGNING_KEY_BYTES:
            raise OSError("internal retirement signing key file is invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("internal retirement signing key changed while opening")
        key = os.read(fd, RETIREMENT_SIGNING_KEY_BYTES + 1)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            len(key) != RETIREMENT_SIGNING_KEY_BYTES
            or _RetirementRecord._stable_identity(opened)
            != _RetirementRecord._stable_identity(after)
            or _RetirementRecord._stable_identity(after)
            != _RetirementRecord._stable_identity(current)
        ):
            raise OSError("internal retirement signing key changed while reading")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError("internal retirement signing key remained visible after consumption")
        os.environ[_INTERNAL_RETIREMENT_KEY_CONSUMED_ENV] = "1"
        return key
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _clear_signing_key_after_fork() -> None:
    global _persistent_signing_key
    _persistent_signing_key = None


def _persistent_relative_path(parent_fd: int, name: str) -> str:
    workspace = os.environ.get(INTERNAL_RETIREMENT_WORKSPACE_ENV, "")
    if not workspace or not os.path.isabs(workspace):
        raise OSError("internal retirement workspace is unavailable")
    try:
        if Path("/proc/self/fd").is_dir():
            parent = os.readlink(f"/proc/self/fd/{parent_fd}")
        else:
            fcntl = require_posix_file_support()
            getpath = getattr(fcntl, "F_GETPATH", None)
            if getpath is None:
                raise OSError("descriptor path lookup is unavailable")
            raw_parent = fcntl.fcntl(parent_fd, getpath, b"\0" * 4096)
            parent = raw_parent.split(b"\0", 1)[0].decode()
    except OSError as exc:
        raise OSError("cannot resolve internal retirement parent") from exc
    parent_info = os.fstat(parent_fd)
    visible_parent = os.stat(parent, follow_symlinks=False)
    if (parent_info.st_dev, parent_info.st_ino) != (
        visible_parent.st_dev,
        visible_parent.st_ino,
    ):
        raise OSError("internal retirement parent changed while resolving")
    workspace_real = os.path.realpath(workspace)
    parent_real = os.path.realpath(parent)
    candidate = os.path.join(parent_real, name)
    try:
        common = os.path.commonpath((workspace_real, candidate))
    except ValueError as exc:
        raise OSError("internal retirement path is outside the workspace") from exc
    if common != workspace_real:
        raise OSError("internal retirement path is outside the workspace")
    relative = os.path.relpath(candidate, workspace_real)
    if relative.startswith("..") or os.path.basename(relative) != name:
        raise OSError("internal retirement path is invalid")
    return relative


_persistent_signing_key = _consume_persistent_signing_key()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_clear_signing_key_after_fork)


def _read_locked_log(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = MAX_RETIREMENT_LOG_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > MAX_RETIREMENT_LOG_BYTES:
        raise OSError("internal retirement log exceeds its byte limit")
    return raw


def _parse_persistent_records(
    raw: bytes,
    signing_key: bytes,
) -> list[tuple[_RetirementRecord, str]]:
    payloads = decode_records(
        raw,
        signing_key,
        record_fields=tuple(_RetirementRecord.__dataclass_fields__),
        max_records=MAX_RETIREMENT_LOG_RECORDS,
        expected_key_bytes=RETIREMENT_SIGNING_KEY_BYTES,
    )
    records: list[tuple[_RetirementRecord, str]] = []
    for payload, supplied_mac in payloads:
        records.append((_RetirementRecord.from_payload(payload), supplied_mac))
    return records


def _load_persistent_records(
    path: str | os.PathLike[str],
    signing_key: bytes,
) -> list[_RetirementRecord]:
    fcntl = require_posix_file_support()
    parent_fd, name = _open_parent_no_symlinks(path)
    fd = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RETIREMENT_LOG_BYTES:
            raise OSError("internal retirement log is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError("internal retirement log changed while opening")
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            locked = os.fstat(fd)
            if (locked.st_dev, locked.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError("internal retirement log changed before reading")
            raw = _read_locked_log(fd)
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

            def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if (
                len(raw) > MAX_RETIREMENT_LOG_BYTES
                or identity(locked) != identity(after)
                or identity(after) != identity(current)
            ):
                raise OSError("internal retirement log changed while reading")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
    return [record for record, _mac in _parse_persistent_records(raw, signing_key)]


def registered_retirement_snapshot(
    workspace: str | os.PathLike[str],
    workspace_fd: int | None = None,
    *,
    persistent_log: str | os.PathLike[str] | None = None,
    persistent_key: bytes | None = None,
    portable_snapshot: bool = False,
) -> tuple[RetirementSnapshot, ...]:
    """Return exact unchanged tombstone identities registered under ``workspace``."""
    root = os.path.realpath(os.path.abspath(workspace))
    if workspace_fd is None:
        root_fd = os.open(root, _directory_flags())
    else:
        root_fd = os.dup(workspace_fd)
    try:
        root_info = os.fstat(root_fd)
        current_root = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or not stat.S_ISDIR(current_root.st_mode)
            or (root_info.st_dev, root_info.st_ino)
            != (current_root.st_dev, current_root.st_ino)
        ):
            raise OSError("retirement scan workspace identity changed")
        with _records_lock:
            records = list(_records)
        if persistent_log is not None:
            signing_key = persistent_key or _persistent_signing_key
            if signing_key is None or len(signing_key) != RETIREMENT_SIGNING_KEY_BYTES:
                raise OSError("internal retirement verification key is unavailable")
            records.extend(_load_persistent_records(persistent_log, signing_key))

        memberships = {record.membership() for record in records}
        latest_metadata: dict[tuple[int, int], tuple[int, int, int, int, int]] = {}
        for record in records:
            latest_metadata[record.inode_identity()] = record.mutation_metadata()
        portable_records = {
            record.relative_path: record
            for record in records
            if record.relative_path
        }

        accepted: list[RetirementSnapshot] = []
        visited = 0

        def scan(parent_fd: int, relative_parent: str) -> None:
            nonlocal visited
            parent_info = os.fstat(parent_fd)
            for name in os.listdir(parent_fd):
                visited += 1
                if visited > MAX_WORKSPACE_SCAN_ENTRIES:
                    raise OSError("retirement workspace scan exceeds its entry limit")
                if name == ".git":
                    continue
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                relative = os.path.join(relative_parent, name) if relative_parent else name
                if stat.S_ISDIR(current.st_mode):
                    if name.startswith(RETIRED_FILE_PREFIX):
                        raise OSError(
                            f"unregistered or modified {RETIRED_FILE_PREFIX}* path: {relative}"
                        )
                    child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                            raise OSError("retirement scan directory changed while opening")
                        scan(child_fd, relative)
                    finally:
                        os.close(child_fd)
                    after_child = os.stat(
                        name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(after_child.st_mode)
                        or (after_child.st_dev, after_child.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    ):
                        raise OSError("retirement scan directory changed after traversal")
                    continue
                if not name.startswith(RETIRED_FILE_PREFIX):
                    continue
                membership = (
                    parent_info.st_dev,
                    parent_info.st_ino,
                    name,
                    current.st_dev,
                    current.st_ino,
                )
                metadata = (
                    stat.S_IFMT(current.st_mode),
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                    current.st_nlink,
                )
                if portable_snapshot:
                    portable = portable_records.get(relative)
                    digest = _hash_regular_entry(parent_fd, name, current)
                    accepted_record = (
                        portable is not None
                        and portable.mode == stat.S_IFMT(current.st_mode)
                        and portable.size == current.st_size
                        and portable.content_sha256 == digest
                    )
                else:
                    accepted_record = (
                        membership in memberships
                        and latest_metadata.get((current.st_dev, current.st_ino)) == metadata
                    )
                if accepted_record:
                    accepted.append(
                        RetirementSnapshot(
                            relative_path=relative,
                            parent_dev=parent_info.st_dev,
                            parent_ino=parent_info.st_ino,
                            file_dev=current.st_dev,
                            file_ino=current.st_ino,
                            mode=stat.S_IFMT(current.st_mode),
                            size=current.st_size,
                            mtime_ns=current.st_mtime_ns,
                            ctime_ns=current.st_ctime_ns,
                            nlink=current.st_nlink,
                        )
                    )
                else:
                    raise OSError(
                        f"unregistered or modified {RETIRED_FILE_PREFIX}* path: {relative}"
                    )

        scan(root_fd, "")
        after_root = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(after_root.st_mode)
            or (after_root.st_dev, after_root.st_ino)
            != (root_info.st_dev, root_info.st_ino)
        ):
            raise OSError("retirement scan workspace changed after traversal")
        return tuple(sorted(accepted, key=lambda item: item.relative_path))
    finally:
        os.close(root_fd)


def _hash_regular_entry(
    parent_fd: int,
    name: str,
    before: os.stat_result,
) -> str:
    if not stat.S_ISREG(before.st_mode):
        raise OSError("retirement snapshot entry is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("retirement snapshot entry changed while opening")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _RetirementRecord._stable_identity(opened)
            != _RetirementRecord._stable_identity(after)
            or _RetirementRecord._stable_identity(after)
            != _RetirementRecord._stable_identity(visible)
        ):
            raise OSError("retirement snapshot entry changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(fd)


def registered_retirement_paths(
    workspace: str | os.PathLike[str],
    workspace_fd: int | None = None,
    *,
    persistent_log: str | os.PathLike[str] | None = None,
    persistent_key: bytes | None = None,
    portable_snapshot: bool = False,
) -> tuple[str, ...]:
    """Return exact unchanged tombstone paths registered under ``workspace``."""
    snapshot = registered_retirement_snapshot(
        workspace,
        workspace_fd,
        persistent_log=persistent_log,
        persistent_key=persistent_key,
        portable_snapshot=portable_snapshot,
    )
    return tuple(item.relative_path for item in snapshot)


__all__ = [
    "INTERNAL_RETIREMENT_LOG_ENV",
    "INTERNAL_RETIREMENT_KEY_FILE_ENV",
    "INTERNAL_RETIREMENT_WORKSPACE_ENV",
    "MAX_RETIREMENT_LOG_BYTES",
    "MAX_RETIREMENT_LOG_RECORDS",
    "RETIRED_FILE_PREFIX",
    "RETIREMENT_SIGNING_KEY_BYTES",
    "RetirementSnapshot",
    "register_verified_retirement",
    "registered_retirement_paths",
    "registered_retirement_snapshot",
]
