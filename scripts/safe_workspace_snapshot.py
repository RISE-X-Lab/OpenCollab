#!/usr/bin/env python3
"""Extract a Docker workspace tar stream without following archive paths."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

MAX_SNAPSHOT_ENTRIES = 500_000
MAX_SNAPSHOT_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_PATH_BYTES = 4096


class SnapshotError(RuntimeError):
    pass


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_root(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    before = absolute.lstat()
    fd = os.open(absolute, _directory_flags())
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(fd)
        raise SnapshotError("snapshot destination is not a stable real directory")
    if os.listdir(fd):
        os.close(fd)
        raise SnapshotError("snapshot destination must start empty")
    return fd


def _member_parts(name: str) -> tuple[str, ...]:
    if not name or "\0" in name or name.startswith("/"):
        raise SnapshotError("snapshot member path is invalid")
    if len(name.encode("utf-8", errors="surrogateescape")) > MAX_SNAPSHOT_PATH_BYTES:
        raise SnapshotError("snapshot member path exceeds its byte limit")
    parts = tuple(part for part in PurePosixPath(name).parts if part != ".")
    if any(part in {"", ".", ".."} for part in parts):
        raise SnapshotError("snapshot member escapes its destination")
    return parts


def _open_parent(root_fd: int, parts: tuple[str, ...]) -> tuple[int, str]:
    if not parts:
        raise SnapshotError("snapshot member has no final component")
    fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=fd)
                os.fsync(fd)
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise SnapshotError("snapshot member parent is not a real directory")
            child_fd = os.open(component, _directory_flags(), dir_fd=fd)
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                os.close(child_fd)
                raise SnapshotError("snapshot member parent changed while opening")
            os.close(fd)
            fd = child_fd
        result = fd
        fd = -1
        return result, parts[-1]
    finally:
        if fd >= 0:
            os.close(fd)


def _write_regular(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    parent_fd: int,
    name: str,
) -> int:
    if member.size < 0 or member.size > MAX_SNAPSHOT_FILE_BYTES:
        raise SnapshotError("snapshot regular file exceeds its size bound")
    source = archive.extractfile(member)
    if source is None:
        raise SnapshotError("snapshot regular file has no payload")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(name, flags, member.mode & 0o777, dir_fd=parent_fd)
    written = 0
    try:
        while written < member.size:
            chunk = source.read(min(65_536, member.size - written))
            if not chunk:
                raise SnapshotError("snapshot regular file ended early")
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise SnapshotError("snapshot regular file write made no progress")
                view = view[count:]
                written += count
        if source.read(1):
            raise SnapshotError("snapshot regular file exceeded its declared size")
        os.fchmod(fd, member.mode & 0o777)
        os.fsync(fd)
        opened = os.fstat(fd)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            or opened.st_size != member.size
        ):
            raise SnapshotError("snapshot regular file changed while writing")
        os.fsync(parent_fd)
        return written
    finally:
        os.close(fd)


def extract_snapshot(
    destination: Path,
    *,
    excluded_top_levels: frozenset[str] = frozenset(),
    reject_symlinks: bool = False,
) -> tuple[int, int]:
    root_fd = _open_root(destination)
    seen: set[tuple[str, ...]] = set()
    entries = 0
    total_bytes = 0
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
            for member in archive:
                parts = _member_parts(member.name)
                if not parts:
                    continue
                if parts[0] in excluded_top_levels:
                    continue
                entries += 1
                if entries > MAX_SNAPSHOT_ENTRIES:
                    raise SnapshotError("snapshot exceeds its entry limit")
                if parts in seen:
                    raise SnapshotError("snapshot contains a duplicate path")
                seen.add(parts)
                parent_fd, name = _open_parent(root_fd, parts)
                try:
                    if member.isdir():
                        try:
                            os.mkdir(name, member.mode & 0o777, dir_fd=parent_fd)
                        except FileExistsError:
                            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                            if not stat.S_ISDIR(current.st_mode):
                                raise SnapshotError("snapshot directory collides with a non-directory")
                        os.fsync(parent_fd)
                    elif member.isreg():
                        total_bytes += _write_regular(archive, member, parent_fd, name)
                        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                            raise SnapshotError("snapshot exceeds its total byte limit")
                    elif member.issym():
                        if reject_symlinks:
                            raise SnapshotError("trusted Git snapshot contains a symbolic link")
                        os.symlink(member.linkname, name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    else:
                        raise SnapshotError("snapshot contains a hardlink or special file")
                finally:
                    os.close(parent_fd)
        os.fsync(root_fd)
        return entries, total_bytes
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError(f"snapshot extraction failed: {exc}") from exc
    finally:
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--exclude-top", action="append", default=[])
    parser.add_argument("--reject-symlinks", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries, total_bytes = extract_snapshot(
            args.destination,
            excluded_top_levels=frozenset(args.exclude_top),
            reject_symlinks=args.reject_symlinks,
        )
    except (OSError, SnapshotError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"entries": entries, "bytes": total_bytes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
