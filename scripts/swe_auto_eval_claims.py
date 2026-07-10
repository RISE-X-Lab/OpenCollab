"""Candidate identity paths and serialized auto-evaluation claims."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
import unicodedata
from pathlib import Path

from opencollab.harness.swe_eval_records import read_bounded_json

from scripts.swe_auto_eval_constants import (
    CLAIM_CONSTRUCTION_GRACE_SECONDS,
    CLAIM_LEASE_SECONDS,
    CLAIM_LEGACY_MAX_AGE_SECONDS,
    MAX_CLAIM_BYTES,
    MAX_SIDE_NAME_BYTES,
    SAFE_FILE_OPEN_RETRIES,
)
from scripts.swe_auto_eval_safe_state import (
    _acquire_exclusive_lock,
    _open_secure_parent,
    _stat_at,
    _write_bytes_atomic_at,
)
from scripts.swebench_process import process_start_identity as _process_start_identity


def _identity_file_stem(task: str) -> str:
    import hashlib

    return hashlib.sha256(task.encode("utf-8", errors="surrogatepass")).hexdigest()


def _validate_side_name(value: object) -> str:
    name = str(value)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("side_name must be one non-dot path component")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name):
        raise ValueError("side_name must not contain control, format, or surrogate characters")
    if len(name.encode("utf-8")) > MAX_SIDE_NAME_BYTES:
        raise ValueError(f"side_name exceeds {MAX_SIDE_NAME_BYTES} UTF-8 bytes")
    return name


def _validate_side_directory(run_dir: Path, side_name: object) -> Path:
    name = _validate_side_name(side_name)
    side_dir = run_dir / name
    try:
        info = side_dir.lstat()
    except FileNotFoundError:
        return side_dir
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"auto-eval side path must be a real directory: {side_dir}")
    return side_dir


def _claim_path(args: argparse.Namespace, task: str) -> Path:
    side_dir = _validate_side_directory(args.run_dir, args.side_name)
    return side_dir / ".opencollab" / "claims" / f"{_identity_file_stem(task)}.json"


def _attempt_path(args: argparse.Namespace, row: dict) -> Path:
    identity = f"{row['task']}\0{row.get('record_id') or ''}\0{row['patch_sha256']}"
    side_name = _validate_side_name(args.side_name)
    return args.run_dir / side_name / ".opencollab" / "attempts" / f"{_identity_file_stem(identity)}.json"


def _attempt_log_path(args: argparse.Namespace, row: dict) -> Path:
    side_name = _validate_side_name(args.side_name)
    return args.run_dir / side_name / ".opencollab" / "logs" / f"{_attempt_path(args, row).stem}.log"


def _pid_is_active(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _claim_lease_is_fresh(claim: dict, file_mtime_ns: int) -> bool:
    now_ns = time.time_ns()
    try:
        expires_at_ns = int(claim.get("lease_expires_at_ns") or 0)
    except (TypeError, ValueError):
        expires_at_ns = 0
    if expires_at_ns > 0:
        return now_ns <= expires_at_ns <= now_ns + int(CLAIM_LEASE_SECONDS * 2 * 1_000_000_000)
    age = (time.time_ns() - file_mtime_ns) / 1_000_000_000
    return 0 <= age < CLAIM_LEGACY_MAX_AGE_SECONDS


def _claim_owner_is_active(claim: dict, file_mtime_ns: int) -> bool:
    try:
        pid = int(claim.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not _pid_is_active(pid):
        return False
    expected = str(claim.get("owner_start_identity") or "")
    current = _process_start_identity(pid)
    if expected and current and expected != current:
        return False
    return _claim_lease_is_fresh(claim, file_mtime_ns)


def _claim_residual_group_is_live(claim: dict, file_mtime_ns: int) -> bool:
    try:
        pgid = int(claim.get("evaluator_pgid") or 0)
    except (TypeError, ValueError):
        return False
    if pgid <= 1 or not _process_group_exists(pgid):
        return False
    expected = str(claim.get("evaluator_start_identity") or "")
    current = _process_start_identity(pgid)
    if expected and current and expected != current:
        return False
    return _claim_lease_is_fresh(claim, file_mtime_ns)


def _read_json(path: Path, *, max_bytes: int | None = None) -> dict:
    document = read_bounded_json(path, max_bytes=max_bytes)
    if document is None:
        return {}
    value, _opened_stat = document
    return value if isinstance(value, dict) else {}


def _claim_is_recent(path: Path) -> bool:
    try:
        opened = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CLAIM_BYTES:
        return False
    age = time.time() - opened.st_mtime
    return 0 <= age < CLAIM_CONSTRUCTION_GRACE_SECONDS


def _read_json_at(parent_fd: int, name: str, *, label: object) -> dict:
    before = _stat_at(parent_fd, name)
    if before is None:
        return {}
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"claim must be a bounded regular file: {label}")
    if before.st_size > MAX_CLAIM_BYTES:
        return {}
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, MAX_CLAIM_BYTES + 1)
        current = _stat_at(parent_fd, name)
    finally:
        os.close(fd)
    if (
        current is None
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or len(raw) > MAX_CLAIM_BYTES
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        or opened.st_size != before.st_size
        or current.st_size != before.st_size
        or opened.st_mtime_ns != before.st_mtime_ns
        or current.st_mtime_ns != before.st_mtime_ns
        or opened.st_ctime_ns != before.st_ctime_ns
        or current.st_ctime_ns != before.st_ctime_ns
    ):
        raise OSError(f"claim changed while reading: {label}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _open_regular_at(
    parent_fd: int,
    name: str,
    flags: int,
    mode: int,
    *,
    label: object,
) -> tuple[int, bool]:
    safe_flags = flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        before = _stat_at(parent_fd, name)
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"refusing non-regular auto-eval file: {label}")
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
        try:
            opened = os.fstat(fd)
            current = _stat_at(parent_fd, name)
            if current is None:
                continue
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise OSError(f"refusing non-regular auto-eval file: {label}")
            opened_identity = (opened.st_dev, opened.st_ino)
            if (current.st_dev, current.st_ino) != opened_identity:
                continue
            if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                continue
            if created:
                os.fsync(parent_fd)
            result_fd = fd
            fd = -1
            return result_fd, created
        except FileNotFoundError:
            pass
        finally:
            if fd >= 0:
                os.close(fd)
    raise OSError(f"auto-eval file did not stabilize while opening: {label}")


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    try:
        return _open_regular_at(parent_fd, name, flags, mode, label=path)
    finally:
        os.close(parent_fd)


def _open_append_binary(path: Path):
    fd, _created = _open_regular_file(path, os.O_WRONLY | os.O_APPEND, 0o644)
    return os.fdopen(fd, "ab")


def _unlink_durable(path: Path) -> None:
    _absolute, parent_fd, name = _open_secure_parent(path, create=False)
    try:
        current = _stat_at(parent_fd, name)
        if current is None:
            return
        if not stat.S_ISREG(current.st_mode):
            raise OSError(f"refusing to unlink non-regular auto-eval file: {path}")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _claim_is_bootstrapping(existing: dict) -> bool:
    if existing.get("status") not in {"claiming", "launching"}:
        return False
    try:
        started_at_ns = int(existing.get("started_at_ns") or 0)
    except (TypeError, ValueError):
        return False
    if started_at_ns <= 0:
        return False
    age_seconds = (time.time_ns() - started_at_ns) / 1_000_000_000
    return 0 <= age_seconds < CLAIM_CONSTRUCTION_GRACE_SECONDS


def _acquire_claim(path: Path, payload: dict) -> tuple[bool, dict]:
    """Serialize claim inspection/reclamation and publish complete JSON atomically."""
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    lock_name = f"{name}.lock"
    lock_fd, _created = _open_regular_at(
        parent_fd,
        lock_name,
        os.O_RDWR,
        0o600,
        label=path.with_name(lock_name),
    )
    locked = False
    try:
        _acquire_exclusive_lock(lock_fd, label=f"claim lock {path.with_name(lock_name)}")
        locked = True
        current = _stat_at(parent_fd, name)
        if current is not None:
            existing = _read_json_at(parent_fd, name, label=path)
            if _claim_owner_is_active(existing, current.st_mtime_ns):
                return False, existing
            if _claim_residual_group_is_live(existing, current.st_mtime_ns):
                return False, existing
            if _claim_is_bootstrapping(existing):
                return False, existing
            # Interoperate safely with a writer from an older process that may
            # have created the destination before finishing its JSON payload.
            # A fresh malformed file is treated as under construction; a stale
            # one can be reclaimed after the grace period.
            age = time.time() - current.st_mtime
            if (
                not existing
                and stat.S_ISREG(current.st_mode)
                and current.st_size <= MAX_CLAIM_BYTES
                and 0 <= age < CLAIM_CONSTRUCTION_GRACE_SECONDS
            ):
                return False, {"status": "claim_in_progress"}
            if not stat.S_ISREG(current.st_mode):
                raise OSError(f"claim must be a regular file: {path}")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)

        # _write_json first creates a complete sibling temp file and then
        # replaces the destination. Contenders cannot observe an empty or
        # half-written claim, while the advisory lock also closes stale-claim
        # check/delete races between cooperating processes.
        claim_payload = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _write_bytes_atomic_at(
            parent_fd,
            name,
            claim_payload,
            label=path,
        )
        return True, payload
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(parent_fd)
