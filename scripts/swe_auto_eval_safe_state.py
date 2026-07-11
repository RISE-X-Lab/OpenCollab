"""Symlink-resistant, durable state-file operations for auto evaluation."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
from pathlib import Path

from opencollab.adapters.safe_files import write_regular_bytes_atomic

from scripts.swe_auto_eval_constants import HARNESS_LOCK_TIMEOUT_SECONDS


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_secure_parent(path: Path, *, create: bool) -> tuple[Path, int, str]:
    absolute = Path(os.path.abspath(path))
    if not absolute.name or absolute.name in {".", ".."}:
        raise OSError(f"invalid auto-eval file path: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError(f"unsafe auto-eval parent component: {path}")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError(f"auto-eval parent must be a real directory: {path}")
            os.close(parent_fd)
            parent_fd = next_fd
        return absolute, parent_fd, absolute.name
    except BaseException:
        os.close(parent_fd)
        raise


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing auto-eval state")
        view = view[written:]


def _acquire_exclusive_lock(fd: int, *, label: str) -> None:
    deadline = time.monotonic() + HARNESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring {label} after {HARNESS_LOCK_TIMEOUT_SECONDS:g}s")
        time.sleep(min(0.01, remaining))


def _write_bytes_atomic_at(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    label: object,
) -> None:
    before = _stat_at(parent_fd, name)
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"refusing non-regular auto-eval destination: {label}")
    target = Path(os.path.abspath(os.fspath(label)))
    if target.name != name:
        raise OSError(f"auto-eval destination label does not match parent entry: {label}")
    parent = os.fstat(parent_fd)
    write_regular_bytes_atomic(
        target,
        payload,
        expected_parent_identity=(parent.st_dev, parent.st_ino),
        expected_target_identity=(before.st_dev, before.st_ino) if before is not None else None,
        require_target_absent=before is None,
    )


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    try:
        _write_bytes_atomic_at(parent_fd, name, payload, label=path)
    finally:
        os.close(parent_fd)


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes_atomic(path, payload)


def _write_markdown(path: Path, summary: dict) -> None:
    lines = [
        "# SWE Auto Eval Status",
        "",
        f"- run_dir: `{summary['run_dir']}`",
        f"- side_name: `{summary['side_name']}`",
        f"- tasks: `{summary['totals']['tasks']}`",
        f"- ready_for_eval: `{summary['totals']['ready_for_eval']}`",
        f"- eval_done: `{summary['totals']['eval_done']}`",
        f"- technical_eval_failed: `{summary['totals']['technical_eval_failed']}`",
        "",
        "| task | state | patch | wf | eval | reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["tasks"]:
        eval_summary = row["eval"]
        eval_label = "none"
        if eval_summary["active_count"]:
            eval_label = "active"
        elif eval_summary["done_count"]:
            eval_label = f"done r={eval_summary['resolved_count']} u={eval_summary['unresolved_count']}"
        elif eval_summary["failed_count"]:
            eval_label = "technical_failed"
        lines.append(
            "| {task} | {state} | {patch} | {wf} | {eval} | {reason} |".format(
                task=row["task"],
                state=row["state"],
                patch=row["patch_len"],
                wf=row["workflow_status"],
                eval=eval_label,
                reason=row["reason"],
            )
        )
    _write_bytes_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))
