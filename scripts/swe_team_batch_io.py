#!/usr/bin/env python3
"""Safe filesystem helpers for ``run_team_batch.sh``."""

from __future__ import annotations

import argparse
import errno
import fcntl
import math
import os
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

MAX_SUMMARY_BYTES = 64 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
LOCK_TIMEOUT_SECONDS = 10.0
SUMMARY_HEADER = (
    "timestamp\tinstance_id\tstatus\tpatch_bytes\twall_seconds\tloop_alert\n"
)
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows = PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows.is_absolute()
        or bool(windows.drive)
        or "/" in value
        or "\\" in value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise ValueError("instance_id must be one safe path component")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_INSTANCE_ID_BYTES:
        raise ValueError("instance_id exceeds its UTF-8 byte limit")
    return value


def validate_tsv_field(value: object, *, label: str) -> str:
    text = str(value)
    if not text or any(character in text for character in "\x00\t\r\n"):
        raise ValueError(f"{label} is unsafe for the batch manifest")
    return text


def lexical_absolute(path: str | Path) -> Path:
    text = os.fspath(path)
    if not text or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in text
    ):
        raise ValueError("batch path is empty or contains unsafe characters")
    return Path(os.path.abspath(text))


def open_directory(path: str | Path, *, create: bool) -> tuple[Path, int]:
    absolute = lexical_absolute(path)
    fd = os.open(absolute.anchor or os.sep, DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError(f"unsafe batch directory component: {absolute}")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError(f"batch path is not a real directory: {absolute}")
            os.close(fd)
            fd = next_fd
        return absolute, fd
    except BaseException:
        os.close(fd)
        raise


def open_parent(path: str | Path, *, create: bool) -> tuple[Path, int, str]:
    absolute = lexical_absolute(path)
    if not absolute.name or absolute.name in {".", ".."}:
        raise OSError(f"invalid batch file path: {absolute}")
    _parent, fd = open_directory(absolute.parent, create=create)
    return absolute, fd, absolute.name


def stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short batch artifact write")
        view = view[written:]


def atomic_write_at(parent_fd: int, name: str, payload: bytes, *, label: object) -> None:
    before = stat_at(parent_fd, name)
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"batch destination must be regular or absent: {label}")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    created = False
    try:
        temp_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        try:
            write_all(temp_fd, payload)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        current = stat_at(parent_fd, name)
        if before is None:
            if current is not None:
                raise OSError(f"batch destination appeared during write: {label}")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError(f"batch destination changed during write: {label}")
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        created = False
        os.fsync(parent_fd)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass


def atomic_write(path: str | Path, payload: bytes) -> Path:
    absolute, parent_fd, name = open_parent(path, create=True)
    try:
        atomic_write_at(parent_fd, name, payload, label=absolute)
    finally:
        os.close(parent_fd)
    return absolute


def open_regular_at(
    parent_fd: int,
    name: str,
    flags: int,
    mode: int,
    *,
    label: object,
) -> tuple[int, bool]:
    safe_flags = (
        flags
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(8):
        before = stat_at(parent_fd, name)
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"batch file must be regular: {label}")
        try:
            if before is None:
                fd = os.open(
                    name,
                    safe_flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            else:
                fd = os.open(name, safe_flags, dir_fd=parent_fd)
                created = False
        except (FileExistsError, FileNotFoundError):
            continue
        opened = os.fstat(fd)
        current = stat_at(parent_fd, name)
        if (
            current is not None
            and stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
            and (
                before is None
                or (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino)
            )
        ):
            if created:
                os.fsync(parent_fd)
            return fd, created
        os.close(fd)
    raise OSError(f"batch file did not stabilize while opening: {label}")


def acquire_lock(fd: int, *, label: object) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring batch lock: {label}")
        time.sleep(min(0.01, remaining))


def read_regular_at(
    parent_fd: int,
    name: str,
    *,
    max_bytes: int,
    label: object,
) -> bytes:
    before = stat_at(parent_fd, name)
    if before is None:
        raise FileNotFoundError(name)
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise OSError(f"batch input must be a bounded regular file: {label}")
    fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        current = stat_at(parent_fd, name)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if (
        current is None
        or len(payload) > max_bytes
        or len(payload) != before.st_size
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        or opened.st_size != before.st_size
        or after.st_size != before.st_size
        or current.st_size != before.st_size
        or opened.st_mtime_ns != before.st_mtime_ns
        or after.st_mtime_ns != before.st_mtime_ns
        or current.st_mtime_ns != before.st_mtime_ns
        or opened.st_ctime_ns != before.st_ctime_ns
        or after.st_ctime_ns != before.st_ctime_ns
        or current.st_ctime_ns != before.st_ctime_ns
    ):
        raise OSError(f"batch input changed while reading: {label}")
    return payload


def _summary_lock(parent_fd: int, summary_name: str) -> int:
    lock_name = f".{summary_name}.lock"
    lock_fd, _created = open_regular_at(
        parent_fd,
        lock_name,
        os.O_RDWR,
        0o600,
        label=lock_name,
    )
    try:
        acquire_lock(lock_fd, label=lock_name)
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def initialize_summary(logs_dir: str | Path) -> tuple[Path, Path]:
    logs, parent_fd = open_directory(logs_dir, create=True)
    summary = logs / "batch.tsv"
    lock_fd = _summary_lock(parent_fd, summary.name)
    try:
        current = stat_at(parent_fd, summary.name)
        if current is None:
            payload = SUMMARY_HEADER.encode("utf-8")
        else:
            raw = read_regular_at(
                parent_fd,
                summary.name,
                max_bytes=MAX_SUMMARY_BYTES,
                label=summary,
            )
            text = raw.decode("utf-8", errors="strict")
            lines = text.splitlines()
            if not lines:
                payload = SUMMARY_HEADER.encode("utf-8")
            elif lines[0].endswith("\tloop_alert"):
                return logs, summary
            else:
                lines[0] += "\tloop_alert"
                for index in range(1, len(lines)):
                    if lines[index].strip():
                        lines[index] += "\tunknown"
                payload = ("\n".join(lines) + "\n").encode("utf-8")
        atomic_write_at(parent_fd, summary.name, payload, label=summary)
        return logs, summary
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(parent_fd)


def prepare_paths(output: str | Path, logs_dir: str | Path) -> tuple[Path, Path, Path]:
    output_path, output_parent_fd, output_name = open_parent(output, create=True)
    try:
        current = stat_at(output_parent_fd, output_name)
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise OSError(f"prediction output must be regular or absent: {output_path}")
    finally:
        os.close(output_parent_fd)
    logs, summary = initialize_summary(logs_dir)
    return output_path, logs, summary


def append_summary(summary: str | Path, fields: list[str]) -> None:
    if len(fields) != 6:
        raise ValueError("batch summary requires six fields")
    safe_fields = [validate_tsv_field(value, label="summary field") for value in fields]
    payload = ("\t".join(safe_fields) + "\n").encode("utf-8")
    summary_path, parent_fd, name = open_parent(summary, create=False)
    lock_fd = _summary_lock(parent_fd, name)
    summary_fd = -1
    try:
        summary_fd, _created = open_regular_at(
            parent_fd,
            name,
            os.O_RDWR | os.O_APPEND,
            0o644,
            label=summary_path,
        )
        size = os.fstat(summary_fd).st_size
        if size + len(payload) > MAX_SUMMARY_BYTES:
            raise OSError("batch summary exceeds its byte limit")
        write_all(summary_fd, payload)
        os.fsync(summary_fd)
        current = stat_at(parent_fd, name)
        opened = os.fstat(summary_fd)
        if current is None or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise OSError("batch summary changed while appending")
        os.fsync(parent_fd)
    finally:
        if summary_fd >= 0:
            os.close(summary_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(parent_fd)


def run_with_log(log_path: str | Path, command: list[str]) -> int:
    if not command:
        raise ValueError("missing batch child command")
    absolute, parent_fd, name = open_parent(log_path, create=False)
    before = stat_at(parent_fd, name)
    if before is not None and not stat.S_ISREG(before.st_mode):
        os.close(parent_fd)
        raise OSError(f"batch log must be regular or absent: {absolute}")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    log_fd = -1
    temp_created = False
    try:
        log_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temp_created = True
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        returncode = proc.wait()
        os.fsync(log_fd)
        current = stat_at(parent_fd, name)
        if before is None:
            if current is not None:
                raise OSError("batch log appeared while child was running")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError("batch log changed while child was running")
        reopened_path, reopened_fd = open_directory(absolute.parent, create=False)
        try:
            rebound = os.fstat(reopened_fd)
            held = os.fstat(parent_fd)
            if (rebound.st_dev, rebound.st_ino) != (held.st_dev, held.st_ino):
                raise OSError(f"batch log parent changed while running: {reopened_path}")
        finally:
            os.close(reopened_fd)
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_created = False
        os.fsync(parent_fd)
        return returncode
    finally:
        if log_fd >= 0:
            os.close(log_fd)
        if temp_created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def read_summary(summary: str | Path) -> bytes:
    absolute, parent_fd, name = open_parent(summary, create=False)
    try:
        return read_regular_at(
            parent_fd,
            name,
            max_bytes=MAX_SUMMARY_BYTES,
            label=absolute,
        )
    finally:
        os.close(parent_fd)


def _timeout_from_env() -> None:
    global LOCK_TIMEOUT_SECONDS
    raw = os.environ.get("OPENCOLLAB_HARNESS_LOCK_TIMEOUT_SECONDS", "10")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("invalid batch lock timeout") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid batch lock timeout")
    LOCK_TIMEOUT_SECONDS = value


def main() -> int:
    _timeout_from_env()
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--logs-dir", required=True)
    append = subparsers.add_parser("append-summary")
    append.add_argument("--summary", required=True)
    append.add_argument("fields", nargs=6)
    run_log = subparsers.add_parser("run-log")
    run_log.add_argument("--log", required=True)
    run_log.add_argument("child", nargs=argparse.REMAINDER)
    display = subparsers.add_parser("display-summary")
    display.add_argument("--summary", required=True)
    args = parser.parse_args()

    if args.command == "prepare":
        output, logs, summary = prepare_paths(args.output, args.logs_dir)
        print(f"{output}\t{logs}\t{summary}")
        return 0
    if args.command == "append-summary":
        append_summary(args.summary, args.fields)
        return 0
    if args.command == "run-log":
        child = args.child[1:] if args.child[:1] == ["--"] else args.child
        return run_with_log(args.log, child)
    if args.command == "display-summary":
        sys.stdout.buffer.write(read_summary(args.summary))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
