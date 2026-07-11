"""Strict records for harness-owned filesystem retirements."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

INTERNAL_RETIREMENT_LOG_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_LOG"
INTERNAL_RETIREMENT_WORKSPACE_ENV = "OPENCOLLAB_INTERNAL_RETIREMENT_WORKSPACE"
RETIRED_FILE_PREFIX = ".opencollab-retired-"
MAX_RETIREMENT_LOG_BYTES = 512 * 1024
MAX_RETIREMENT_LOG_RECORDS = 1024
MAX_WORKSPACE_SCAN_ENTRIES = 100_000


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
    relative_path: str = ""

    @classmethod
    def capture(cls, parent_fd: int, name: str, *, relative_path: str = "") -> _RetirementRecord:
        _validate_name(name)
        parent = os.fstat(parent_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode) or not stat.S_ISREG(current.st_mode):
            raise OSError("retirement record must identify one regular file")
        return cls(
            parent.st_dev,
            parent.st_ino,
            name,
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
            current.st_nlink,
            relative_path,
        )

    @classmethod
    def from_payload(cls, payload: object) -> _RetirementRecord:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("retirement log record fields are invalid")
        record = cls(**payload)
        _validate_name(record.name)
        if record.relative_path:
            normalized = os.path.normpath(record.relative_path)
            if (
                os.path.isabs(record.relative_path)
                or normalized == ".."
                or normalized.startswith(f"..{os.sep}")
                or os.path.basename(normalized) != record.name
            ):
                raise ValueError("retirement log record path is invalid")
        for field in fields - {"name", "relative_path"}:
            value = getattr(record, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("retirement log record numeric field is invalid")
        return record

    def identity(self) -> tuple[int, ...]:
        return (
            self.parent_dev,
            self.parent_ino,
            self.file_dev,
            self.file_ino,
            self.mode,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
            self.nlink,
        )


_lock = threading.Lock()
_records: list[_RetirementRecord] = []


def _validate_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name.startswith(RETIRED_FILE_PREFIX)
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise ValueError("retirement name is outside the reserved namespace")


def _relative_path(parent_fd: int, name: str) -> str:
    workspace = os.environ.get(INTERNAL_RETIREMENT_WORKSPACE_ENV, "")
    if not workspace or not os.path.isabs(workspace):
        raise OSError("internal retirement workspace is unavailable")
    if Path("/proc/self/fd").is_dir():
        parent = os.readlink(f"/proc/self/fd/{parent_fd}")
    else:
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is None:
            raise OSError("descriptor path lookup is unavailable")
        raw = fcntl.fcntl(parent_fd, getpath, b"\0" * 1024)
        parent = raw.split(b"\0", 1)[0].decode()
    root = os.path.realpath(workspace)
    candidate = os.path.realpath(os.path.join(parent, name))
    if os.path.commonpath((root, candidate)) != root:
        raise OSError("internal retirement path is outside the workspace")
    relative = os.path.relpath(candidate, root)
    if os.path.basename(relative) != name:
        raise OSError("internal retirement path is invalid")
    return relative


def _append_record(path: str, record: _RetirementRecord) -> None:
    payload = json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size + len(payload) > MAX_RETIREMENT_LOG_BYTES:
            raise OSError("internal retirement log exceeds its bound")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _records_from_raw(raw: bytes) -> list[_RetirementRecord]:
    if raw and not raw.endswith(b"\n"):
        raise OSError("internal retirement log has a partial record")
    lines = raw.splitlines()
    if len(lines) > MAX_RETIREMENT_LOG_RECORDS:
        raise OSError("internal retirement log has too many records")
    records: list[_RetirementRecord] = []
    for line in lines:
        try:
            payload = json.loads(line, object_pairs_hook=_unique_object)
            records.append(_RetirementRecord.from_payload(payload))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OSError("internal retirement log is malformed") from exc
    return records


def _read_locked_records(fd: int) -> list[_RetirementRecord]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RETIREMENT_LOG_BYTES:
        raise OSError("internal retirement log is invalid")
    os.lseek(fd, 0, os.SEEK_SET)
    return _records_from_raw(os.read(fd, MAX_RETIREMENT_LOG_BYTES + 1))


def _write_locked_records(fd: int, records: list[_RetirementRecord]) -> None:
    if len(records) > MAX_RETIREMENT_LOG_RECORDS:
        raise OSError("internal retirement log has too many records")
    payload = b"".join(
        json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )
    if len(payload) > MAX_RETIREMENT_LOG_BYTES:
        raise OSError("internal retirement log exceeds its bound")
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("internal retirement log rewrite made no progress")
        view = view[written:]
    os.ftruncate(fd, len(payload))
    os.fsync(fd)


def register_verified_retirement(parent_fd: int, retired_name: str) -> None:
    """Record one verified internal tombstone before patch extraction."""
    log = os.environ.get(INTERNAL_RETIREMENT_LOG_ENV, "")
    relative = _relative_path(parent_fd, retired_name) if log else ""
    record = _RetirementRecord.capture(parent_fd, retired_name, relative_path=relative)
    if log:
        _append_record(log, record)
    with _lock:
        _records.append(record)
        del _records[:-MAX_RETIREMENT_LOG_RECORDS]


def _load_records(path: str | os.PathLike[str]) -> list[_RetirementRecord]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        records = _read_locked_records(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return records


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate retirement log field")
        result[key] = value
    return result


def verified_retirement_identities(
    parent_fd: int,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Return the latest registered identity for each tombstone in one parent."""
    parent = os.fstat(parent_fd)
    if not stat.S_ISDIR(parent.st_mode):
        raise OSError("retirement parent descriptor is not a directory")
    with _lock:
        records = list(_records)
    log = os.environ.get(INTERNAL_RETIREMENT_LOG_ENV, "")
    if log:
        records.extend(_load_records(log))
    expected_parent = (parent.st_dev, parent.st_ino)
    identities: dict[str, tuple[int, ...]] = {}
    for record in records:
        if (record.parent_dev, record.parent_ino) == expected_parent:
            identities[record.name] = record.identity()
    return tuple(identities.items())


def forget_verified_retirements(parent_fd: int, names: tuple[str, ...]) -> None:
    """Forget tombstones removed after descriptor and registry verification."""
    if not names:
        return
    for name in names:
        _validate_name(name)
    parent = os.fstat(parent_fd)
    if not stat.S_ISDIR(parent.st_mode):
        raise OSError("retirement parent descriptor is not a directory")
    expected_parent = (parent.st_dev, parent.st_ino)
    removed = set(names)

    log = os.environ.get(INTERNAL_RETIREMENT_LOG_ENV, "")
    if log:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(log, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            records = _read_locked_records(fd)
            retained = [
                record
                for record in records
                if not (
                    (record.parent_dev, record.parent_ino) == expected_parent
                    and record.name in removed
                )
            ]
            _write_locked_records(fd, retained)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    with _lock:
        _records[:] = [
            record
            for record in _records
            if not (
                (record.parent_dev, record.parent_ino) == expected_parent
                and record.name in removed
            )
        ]


def registered_retirement_paths(
    workspace: str | os.PathLike[str],
    workspace_fd: int | None = None,
    *,
    persistent_log: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Return exact registered tombstones and reject every unknown reserved path."""
    root = os.path.realpath(os.path.abspath(workspace))
    root_fd = os.dup(workspace_fd) if workspace_fd is not None else os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_opened = os.fstat(root_fd)
        root_visible = os.stat(root, follow_symlinks=False)
        root_identity = (root_opened.st_dev, root_opened.st_ino)
        if not stat.S_ISDIR(root_opened.st_mode) or (root_visible.st_dev, root_visible.st_ino) != root_identity:
            raise OSError("retirement workspace changed before scan")
        with _lock:
            records = list(_records)
        if persistent_log is not None:
            records.extend(_load_records(persistent_log))
        by_path = {record.relative_path: record for record in records if record.relative_path}
        by_inode = {
            (record.parent_dev, record.parent_ino, record.name, record.file_dev, record.file_ino): record
            for record in records
        }
        accepted: list[str] = []
        visited = 0

        def scan(parent_fd: int, relative_parent: str) -> None:
            nonlocal visited
            parent = os.fstat(parent_fd)
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
                        raise OSError(f"unregistered or modified {RETIRED_FILE_PREFIX}* path: {relative}")
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                            raise OSError("retirement scan directory changed before traversal")
                        scan(child, relative)
                    finally:
                        os.close(child)
                    visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                        raise OSError("retirement scan directory changed after traversal")
                    continue
                if not name.startswith(RETIRED_FILE_PREFIX):
                    continue
                identity = (parent.st_dev, parent.st_ino, name, current.st_dev, current.st_ino)
                record = by_path.get(relative) or by_inode.get(identity)
                actual = (
                    parent.st_dev,
                    parent.st_ino,
                    current.st_dev,
                    current.st_ino,
                    stat.S_IFMT(current.st_mode),
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                    current.st_nlink,
                )
                if record is None or record.identity() != actual:
                    raise OSError(f"unregistered or modified {RETIRED_FILE_PREFIX}* path: {relative}")
                accepted.append(relative)

        scan(root_fd, "")
        root_visible = os.stat(root, follow_symlinks=False)
        if (root_visible.st_dev, root_visible.st_ino) != root_identity:
            raise OSError("retirement workspace changed after scan")
        return tuple(sorted(accepted))
    finally:
        os.close(root_fd)


__all__ = [
    "INTERNAL_RETIREMENT_LOG_ENV",
    "INTERNAL_RETIREMENT_WORKSPACE_ENV",
    "MAX_RETIREMENT_LOG_BYTES",
    "MAX_RETIREMENT_LOG_RECORDS",
    "RETIRED_FILE_PREFIX",
    "forget_verified_retirements",
    "register_verified_retirement",
    "registered_retirement_paths",
    "verified_retirement_identities",
]
