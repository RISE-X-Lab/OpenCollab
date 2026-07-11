"""Bounded report discovery and stable report fingerprints."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from opencollab.harness.swe_eval_records import read_bounded_json

from scripts.swe_auto_eval_constants import (
    MAX_REPORT_DOCUMENT_BYTES,
    MAX_REPORT_SCAN_BYTES,
    MAX_REPORT_SCAN_ENTRIES,
    MAX_REPORT_SCAN_FILES,
)


def _open_real_directory(path: Path) -> int:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"auto-eval report directory cannot be inspected: {path}") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"auto-eval report directory must be real: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"auto-eval report directory cannot be opened: {path}") from exc
    opened = os.fstat(fd)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise ValueError(f"auto-eval report directory changed while opening: {path}")
    return fd


def _iter_report_json_paths(side_dir: Path):
    pending = [side_dir]
    scanned_entries = 0
    while pending:
        directory = pending.pop()
        fd = _open_real_directory(directory)
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    scanned_entries += 1
                    if scanned_entries > MAX_REPORT_SCAN_ENTRIES:
                        raise ValueError(f"auto-eval report scan exceeds {MAX_REPORT_SCAN_ENTRIES} directory entries")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    path = directory / entry.name
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode) and entry.name.endswith(".json"):
                        yield path
        finally:
            os.close(fd)


def _report_fingerprints(side_dir: Path, task: str) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    try:
        root_info = side_dir.lstat()
    except FileNotFoundError:
        return fingerprints
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"auto-eval report root must be a real directory: {side_dir}")
    scanned_files = 0
    scanned_bytes = 0
    for path in _iter_report_json_paths(side_dir):
        scanned_files += 1
        if scanned_files > MAX_REPORT_SCAN_FILES:
            raise ValueError(f"auto-eval report scan exceeds {MAX_REPORT_SCAN_FILES} JSON files")
        try:
            entry_info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(entry_info.st_mode):
            continue
        if entry_info.st_size > MAX_REPORT_DOCUMENT_BYTES:
            raise ValueError(f"auto-eval report exceeds byte limit: {path}")
        scanned_bytes += entry_info.st_size
        if scanned_bytes > MAX_REPORT_SCAN_BYTES:
            raise ValueError(f"auto-eval report scan exceeds {MAX_REPORT_SCAN_BYTES} bytes")
        document = read_bounded_json(path, max_bytes=MAX_REPORT_DOCUMENT_BYTES)
        if document is None or not isinstance(document[0], dict):
            continue
        payload, opened = document
        scanned_bytes += max(0, opened.st_size - entry_info.st_size)
        if scanned_bytes > MAX_REPORT_SCAN_BYTES:
            raise ValueError(f"auto-eval report scan exceeds {MAX_REPORT_SCAN_BYTES} bytes")
        if payload.get("schema") in {
            "opencollab.swe_eval_attempt.v1",
            "opencollab.swe_eval_claim.v1",
        }:
            continue
        if str(payload.get("instance_id") or payload.get("task_id") or payload.get("task") or "") != task:
            item = payload.get(task)
            if not isinstance(item, dict):
                continue
        try:
            relative = str(path.relative_to(side_dir))
        except (OSError, ValueError):
            continue
        fingerprints[relative] = f"{opened.st_mtime_ns}:{opened.st_ctime_ns}:{opened.st_size}:{opened.st_ino}"
    return fingerprints
