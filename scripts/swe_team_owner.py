#!/usr/bin/env python3
"""Container ownership records and lock guard for ``start_team_run.sh``."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import time
import uuid
from pathlib import Path

from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    read_regular_bytes,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)

try:
    from scripts import swe_team_batch_io as batch_io
except ImportError:  # Direct execution places this module's directory on sys.path.
    import swe_team_batch_io as batch_io

OWNER_SCHEMA = "opencollab.team-owner.v1"
MAX_OWNER_BYTES = 4096
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
CONTAINER_NAME_PATTERN = re.compile(r"oc-team-[A-Za-z0-9_.-]+")
OWNER_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")


def _owner_payload(
    path: Path,
) -> tuple[dict[str, object], tuple[int, int], int]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_OWNER_BYTES:
            raise OSError("owner marker is not a bounded regular file")
        raw = read_regular_bytes(path, max_bytes=MAX_OWNER_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        after = path.lstat()
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("owner marker is not a bounded regular file") from exc
    identity = (before.st_dev, before.st_ino)
    if (
        not isinstance(payload, dict)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != identity
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
    ):
        raise ValueError("owner marker identity is invalid")
    return payload, identity, stat.S_IMODE(before.st_mode)


def validate_container_id(raw: str) -> str:
    value = raw.strip()
    if CONTAINER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("docker run returned an invalid container id")
    return value


def validate_inspect_output(
    output: str,
    container_name: str,
    expected_id: str,
    expected_nonce: str,
) -> str:
    parts = output.strip().split("\t")
    if len(parts) != 3:
        raise ValueError("container ownership inspect output is malformed")
    actual_id, actual_name, actual_nonce = parts
    if CONTAINER_ID_PATTERN.fullmatch(actual_id) is None:
        raise ValueError("container ownership inspect returned an invalid id")
    if actual_name != "/" + container_name or actual_nonce != expected_nonce:
        raise ValueError("container ownership label or name mismatch")
    if (
        CONTAINER_ID_PATTERN.fullmatch(expected_id) is not None
        and actual_id != expected_id
    ):
        raise ValueError("container ownership id mismatch")
    return actual_id


def _serialized(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def create_marker(
    path: Path,
    container_name: str,
    session_key: str,
    owner_nonce: str,
) -> None:
    payload = {
        "schema": OWNER_SCHEMA,
        "session_key": session_key,
        "container_name": container_name,
        "container_id": "",
        "owner_nonce": owner_nonce,
    }
    create_regular_bytes_atomic(
        path,
        _serialized(payload),
        max_bytes=MAX_OWNER_BYTES,
        mode=0o600,
    )


def read_marker(path: Path, session_key: str) -> tuple[str, str, str]:
    payload, _identity, _mode = _owner_payload(path)
    container_name = str(payload.get("container_name") or "")
    owner_nonce = str(payload.get("owner_nonce") or "")
    container_id = str(payload.get("container_id") or "")
    if (
        payload.get("schema") != OWNER_SCHEMA
        or payload.get("session_key") != session_key
        or CONTAINER_NAME_PATTERN.fullmatch(container_name) is None
        or OWNER_NONCE_PATTERN.fullmatch(owner_nonce) is None
        or (
            bool(container_id)
            and CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        )
    ):
        raise ValueError("owner marker identity is invalid")
    return container_name, container_id, owner_nonce


def _matches(
    payload: dict[str, object],
    session_key: str,
    container_name: str,
    container_id: str,
    owner_nonce: str,
) -> bool:
    return (
        payload.get("schema") == OWNER_SCHEMA
        and payload.get("session_key") == session_key
        and payload.get("container_name") == container_name
        and payload.get("owner_nonce") == owner_nonce
        and (not container_id or (payload.get("container_id") or "") == container_id)
    )


def remove_marker(
    path: Path,
    session_key: str,
    container_name: str,
    container_id: str,
    owner_nonce: str,
    *,
    require_match: bool,
) -> bool:
    if not os.path.lexists(path):
        return False
    payload, identity, _mode = _owner_payload(path)
    matched = _matches(
        payload,
        session_key,
        container_name,
        container_id,
        owner_nonce,
    )
    if not matched:
        if require_match:
            raise ValueError("owner marker changed during recovery")
        return False
    return unlink_regular_file_durable(
        path,
        expected_target_identity=identity,
    )


def bind_container_id(
    path: Path,
    session_key: str,
    container_name: str,
    owner_nonce: str,
    container_id: str,
) -> None:
    container_id = validate_container_id(container_id)
    payload, identity, mode = _owner_payload(path)
    if (
        payload.get("schema") != OWNER_SCHEMA
        or payload.get("session_key") != session_key
        or payload.get("container_name") != container_name
        or payload.get("owner_nonce") != owner_nonce
        or payload.get("container_id") not in {"", container_id}
    ):
        raise ValueError("owner marker changed before container id binding")
    payload["container_id"] = container_id
    write_regular_bytes_atomic(
        path,
        _serialized(payload),
        max_bytes=MAX_OWNER_BYTES,
        mode=mode,
        expected_target_identity=identity,
    )


def _write_lock_status(path: Path, payload: dict[str, object]) -> None:
    write_regular_bytes_atomic(
        path,
        _serialized(payload),
        max_bytes=MAX_OWNER_BYTES,
        mode=0o600,
    )


def hold_lock(lock_path: Path, status_path: Path, parent_pid: int) -> int:
    _absolute, parent_fd, name = batch_io.open_parent(lock_path, create=True)
    fd = -1
    try:
        fd, _created = batch_io.open_regular_at(
            parent_fd,
            name,
            os.O_RDWR,
            0o600,
            label=lock_path,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            _write_lock_status(
                status_path,
                {"status": "error", "error": str(exc)},
            )
            return 1
        _write_lock_status(status_path, {"status": "locked"})
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while os.getppid() == parent_pid:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        return 0
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        os.close(parent_fd)


def read_lock_status(path: Path) -> str:
    payload = json.loads(read_regular_bytes(path, max_bytes=MAX_OWNER_BYTES))
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"locked", "error"}:
        raise ValueError("owner lock status is invalid")
    return status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("validate-inspect")
    inspect.add_argument("output")
    inspect.add_argument("container_name")
    inspect.add_argument("expected_id")
    inspect.add_argument("expected_nonce")
    cid = commands.add_parser("validate-cid")
    cid.add_argument("raw")
    commands.add_parser("new-nonce")
    create = commands.add_parser("create-marker")
    create.add_argument("path", type=Path)
    create.add_argument("container_name")
    create.add_argument("session_key")
    create.add_argument("owner_nonce")
    read = commands.add_parser("read-marker")
    read.add_argument("path", type=Path)
    read.add_argument("session_key")
    remove = commands.add_parser("remove-marker")
    remove.add_argument("path", type=Path)
    remove.add_argument("session_key")
    remove.add_argument("container_name")
    remove.add_argument("container_id")
    remove.add_argument("owner_nonce")
    remove.add_argument("--require-match", action="store_true")
    bind = commands.add_parser("bind-cid")
    bind.add_argument("path", type=Path)
    bind.add_argument("session_key")
    bind.add_argument("container_name")
    bind.add_argument("owner_nonce")
    bind.add_argument("container_id")
    guard = commands.add_parser("hold-lock")
    guard.add_argument("lock_path", type=Path)
    guard.add_argument("status_path", type=Path)
    guard.add_argument("parent_pid", type=int)
    status = commands.add_parser("read-lock-status")
    status.add_argument("status_path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-inspect":
            print(
                validate_inspect_output(
                    args.output,
                    args.container_name,
                    args.expected_id,
                    args.expected_nonce,
                )
            )
        elif args.command == "validate-cid":
            print(validate_container_id(args.raw))
        elif args.command == "new-nonce":
            print(uuid.uuid4().hex)
        elif args.command == "create-marker":
            create_marker(
                args.path,
                args.container_name,
                args.session_key,
                args.owner_nonce,
            )
        elif args.command == "read-marker":
            print("|".join(read_marker(args.path, args.session_key)))
        elif args.command == "remove-marker":
            remove_marker(
                args.path,
                args.session_key,
                args.container_name,
                args.container_id,
                args.owner_nonce,
                require_match=args.require_match,
            )
        elif args.command == "bind-cid":
            bind_container_id(
                args.path,
                args.session_key,
                args.container_name,
                args.owner_nonce,
                args.container_id,
            )
        elif args.command == "hold-lock":
            return hold_lock(args.lock_path, args.status_path, args.parent_pid)
        elif args.command == "read-lock-status":
            print(read_lock_status(args.status_path))
        else:
            raise AssertionError(args.command)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
