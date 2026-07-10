"""Ownership-verified remote cleanup for the SWE v1 pro-lite runner."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

RUNNER_OWNER_SCHEMA = "opencollab.prolite_runner_owner.v1"
EVAL_CONTAINER_SCHEMA = "opencollab.prolite_eval_container.v1"
GENERATION_CONTAINER_SCHEMA_VERSION = 1
EVAL_OWNER_LABEL = "opencollab.prolite.owner_nonce"
EVAL_SCHEMA_LABEL = "opencollab.prolite.schema"
EVAL_SCHEMA_LABEL_VALUE = "direct-eval-v1"
GENERATION_OWNER_LABEL = "opencollab.harness.owner-token"
MAX_OWNER_BYTES = 4096
MAX_CONTAINER_MARKER_BYTES = 1024 * 1024
MAX_CONTAINER_REFERENCE_BYTES = 128
MAX_CONTAINER_MARKERS = 4096
MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_DIRECTORIES = 10_000
FULL_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
OWNER_NONCE_RE = re.compile(r"[0-9a-f]{32}")
CONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MISSING_CONTAINER_RE = re.compile(r"no such (?:container|object)|not found", re.IGNORECASE)


class CleanupInputError(RuntimeError):
    """A cleanup input could not establish the claimed ownership."""


def read_bounded_regular(path: pathlib.Path, *, max_bytes: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
        raise CleanupInputError(f"cleanup marker is not a bounded regular file: {path}")
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, max_bytes + 1)
        current = path.lstat()
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or len(raw) > max_bytes
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise CleanupInputError(f"cleanup marker changed while reading: {path}")
    return raw


def read_bounded_json(path: pathlib.Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        value = json.loads(read_bounded_regular(path, max_bytes=max_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupInputError(f"cleanup marker is invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CleanupInputError(f"cleanup marker must contain an object: {path}")
    return value


def read_runner_owner(path: pathlib.Path) -> dict[str, Any]:
    value = read_bounded_json(path, max_bytes=MAX_OWNER_BYTES)
    pid = value.get("pid")
    start_identity = value.get("start_identity")
    owner_nonce = value.get("owner_nonce")
    if (
        value.get("schema") != RUNNER_OWNER_SCHEMA
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or not isinstance(start_identity, str)
        or not start_identity
        or not isinstance(owner_nonce, str)
        or OWNER_NONCE_RE.fullmatch(owner_nonce) is None
    ):
        raise CleanupInputError("runner owner record is invalid")
    return value


def _full_container_id(value: object) -> str:
    normalized = str(value or "").lower()
    if FULL_CONTAINER_ID_RE.fullmatch(normalized) is None:
        raise CleanupInputError("container marker does not contain a complete container id")
    return normalized


def _owner_nonce(value: object) -> str:
    normalized = str(value or "")
    if OWNER_NONCE_RE.fullmatch(normalized) is None:
        raise CleanupInputError("container marker owner nonce is invalid")
    return normalized


def read_eval_container_marker(
    path: pathlib.Path,
    *,
    expected_runner_nonce: str,
) -> dict[str, str]:
    value = read_bounded_json(path, max_bytes=MAX_CONTAINER_MARKER_BYTES)
    container_name = str(value.get("container_name") or "")
    nonce = _owner_nonce(value.get("owner_nonce"))
    if (
        value.get("schema") != EVAL_CONTAINER_SCHEMA
        or value.get("state") != "active"
        or value.get("owner_label") != EVAL_OWNER_LABEL
        or value.get("owner_schema_label") != EVAL_SCHEMA_LABEL
        or value.get("owner_schema") != EVAL_SCHEMA_LABEL_VALUE
        or nonce != expected_runner_nonce
        or CONTAINER_NAME_RE.fullmatch(container_name) is None
    ):
        raise CleanupInputError("eval container marker ownership is invalid")
    return {
        "kind": "eval",
        "container_id": _full_container_id(value.get("container_id")),
        "container_name": container_name,
        "owner_nonce": nonce,
        "owner_label": EVAL_OWNER_LABEL,
        "owner_schema_label": EVAL_SCHEMA_LABEL,
        "owner_schema": EVAL_SCHEMA_LABEL_VALUE,
        "marker_path": str(path),
    }


def read_generation_container_marker(path: pathlib.Path) -> dict[str, str]:
    value = read_bounded_json(path, max_bytes=MAX_CONTAINER_MARKER_BYTES)
    container_name = str(value.get("container_name") or "")
    nonce = _owner_nonce(value.get("owner_token"))
    if (
        value.get("schema_version") != GENERATION_CONTAINER_SCHEMA_VERSION
        or value.get("state") not in {"active", "preservation_required", "candidate_staged", "kept"}
        or CONTAINER_NAME_RE.fullmatch(container_name) is None
    ):
        raise CleanupInputError("generation container marker ownership is invalid")
    return {
        "kind": "generation",
        "container_id": _full_container_id(value.get("container_id")),
        "container_name": container_name,
        "owner_nonce": nonce,
        "owner_label": GENERATION_OWNER_LABEL,
        "marker_path": str(path),
    }


def _bounded_paths(base: pathlib.Path) -> list[pathlib.Path]:
    try:
        root_stat = base.lstat()
    except OSError as exc:
        raise CleanupInputError(f"cleanup root is unavailable: {base}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise CleanupInputError(f"cleanup root must be a directory: {base}")
    stack = [base]
    paths: list[pathlib.Path] = []
    entries_seen = 0
    directories_seen = 1
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_SCAN_ENTRIES:
                        raise CleanupInputError("cleanup directory entry count exceeds its bound")
                    path = pathlib.Path(entry.path)
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        raise CleanupInputError(f"cleanup entry is unverifiable: {path}") from exc
                    if is_directory:
                        directories_seen += 1
                        if directories_seen > MAX_SCAN_DIRECTORIES:
                            raise CleanupInputError("cleanup directory count exceeds its bound")
                        stack.append(path)
                    else:
                        paths.append(path)
        except CleanupInputError:
            raise
        except OSError as exc:
            raise CleanupInputError(f"cleanup directory is unreadable: {directory}") from exc
    return paths


def _marker_paths(
    base: pathlib.Path,
) -> tuple[list[pathlib.Path], list[pathlib.Path], list[pathlib.Path]]:
    eval_paths: list[pathlib.Path] = []
    generation_paths: list[pathlib.Path] = []
    reference_paths: list[pathlib.Path] = []
    for path in _bounded_paths(base):
        if path.name == "container.marker.json":
            eval_paths.append(path)
        elif path.parent.name == "container_owners" and path.parent.parent.name == ".opencollab":
            generation_paths.append(path)
        elif path.name in {"container.id", "container.cid"}:
            reference_paths.append(path)
        if len(eval_paths) + len(generation_paths) > MAX_CONTAINER_MARKERS:
            raise CleanupInputError("container marker count exceeds its bound")
    return sorted(eval_paths), sorted(generation_paths), sorted(reference_paths)


def discover_owned_containers(
    base: pathlib.Path,
    *,
    runner_nonce: str,
) -> tuple[list[dict[str, str]], list[str]]:
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        eval_paths, generation_paths, reference_paths = _marker_paths(base)
    except (OSError, CleanupInputError) as exc:
        return [], [str(exc)]
    for path in eval_paths:
        try:
            candidates.append(read_eval_container_marker(path, expected_runner_nonce=runner_nonce))
        except (OSError, CleanupInputError) as exc:
            errors.append(f"{path}: {exc}")
    for path in generation_paths:
        try:
            candidates.append(read_generation_container_marker(path))
        except (OSError, CleanupInputError) as exc:
            errors.append(f"{path}: {exc}")

    by_id: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        cid = candidate["container_id"]
        previous = by_id.get(cid)
        if previous is not None and previous["owner_nonce"] != candidate["owner_nonce"]:
            errors.append(f"conflicting ownership markers for container {cid}")
            continue
        by_id[cid] = candidate

    trusted_ids = set(by_id)
    for path in reference_paths:
        try:
            raw = read_bounded_regular(path, max_bytes=MAX_CONTAINER_REFERENCE_BYTES)
            cid = _full_container_id(raw.decode("ascii").strip())
        except (OSError, UnicodeDecodeError, CleanupInputError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if cid not in trusted_ids:
            errors.append(f"{path}: container id has no ownership marker")
    return list(by_id.values()), errors


def _docker_inspect(candidate: dict[str, str]) -> dict[str, Any]:
    container_id = candidate["container_id"]
    owner_nonce = candidate["owner_nonce"]
    owner_label = candidate["owner_label"]
    inspect_format = '{{.Id}}{{printf "\\t"}}' + f'{{{{ index .Config.Labels "{owner_label}" }}}}'
    expected_parts = 2
    owner_schema_label = candidate.get("owner_schema_label")
    if owner_schema_label:
        inspect_format += '{{printf "\\t"}}' + f'{{{{ index .Config.Labels "{owner_schema_label}" }}}}'
        expected_parts = 3
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                inspect_format,
                "--",
                container_id,
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "unverifiable", "error": repr(exc)}
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        if MISSING_CONTAINER_RE.search(detail):
            return {"state": "absent", "detail": detail[:500]}
        return {
            "state": "unverifiable",
            "returncode": result.returncode,
            "detail": detail[:500],
        }
    parts = result.stdout.strip().split("\t")
    if len(parts) != expected_parts:
        return {"state": "unverifiable", "detail": "unexpected docker inspect output"}
    inspected_id, inspected_nonce = parts[:2]
    schema_matches = expected_parts == 2 or parts[2] == candidate.get("owner_schema")
    if inspected_id.lower() != container_id or inspected_nonce != owner_nonce or not schema_matches:
        return {
            "state": "foreign",
            "inspected_id": inspected_id[:128],
            "owner_label_matches": inspected_nonce == owner_nonce,
            "owner_schema_matches": schema_matches,
        }
    return {"state": "matching"}


def remove_owned_container(candidate: dict[str, str]) -> dict[str, Any]:
    cid = candidate["container_id"]
    inspected = _docker_inspect(candidate)
    if inspected.get("state") == "absent":
        return {**candidate, "ok": True, "status": "already_absent", "inspect": inspected}
    if inspected.get("state") != "matching":
        return {
            **candidate,
            "ok": False,
            "status": "owner_unverified",
            "inspect": inspected,
        }
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", "--", cid],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**candidate, "ok": False, "status": "remove_failed", "error": repr(exc)}
    after = _docker_inspect(candidate)
    ok = after.get("state") == "absent"
    return {
        **candidate,
        "ok": ok,
        "status": "removed" if ok else "remove_failed",
        "remove_returncode": removed.returncode,
        "remove_stdout": removed.stdout.strip()[:200],
        "remove_stderr": removed.stderr.strip()[:200],
        "inspect_after": after,
    }


def process_start_identity(pid: int, errors: list[str]) -> str:
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        if len(remainder) > 19 and remainder[19].isdigit():
            return f"proc:{remainder[19]}"
    except (OSError, IndexError) as exc:
        errors.append(repr(exc))
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(repr(exc))
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""


def scan_processes(owner_nonce: str, *, excluded: set[int], errors: list[str]) -> list[tuple[int, int, str]]:
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,pgid=,args="],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(repr(exc))
        return []
    rows: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid in excluded:
            continue
        try:
            tokens = shlex.split(parts[2])
        except ValueError:
            continue
        if owner_nonce in tokens:
            rows.append((pid, pgid, parts[2]))
    return rows


def _send_pid(pid: int, sig: signal.Signals) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def cleanup_run(base: pathlib.Path) -> dict[str, Any]:
    me, parent = os.getpid(), os.getppid()
    errors: list[str] = []
    killed: list[dict[str, Any]] = []
    try:
        owner = read_runner_owner(base / "runner.pid")
    except (FileNotFoundError, OSError, CleanupInputError) as exc:
        owner = None
        errors.append(f"runner owner unverified: {exc}")

    owner_nonce = str((owner or {}).get("owner_nonce") or "")
    runner_pid = int((owner or {}).get("pid") or 0)
    expected_start = str((owner or {}).get("start_identity") or "")
    current_start = process_start_identity(runner_pid, errors) if runner_pid > 1 else ""
    owner_matches = bool(
        runner_pid > 1 and runner_pid not in {me, parent} and expected_start and current_start == expected_start
    )
    if runner_pid > 1 and current_start and not owner_matches:
        errors.append("runner PID/start identity mismatch")
    if owner_matches and _send_pid(runner_pid, signal.SIGTERM):
        killed.append({"pid": runner_pid, "signal": "TERM"})

    excluded = {me, parent}
    for sig_name, sig_value, delay in (
        ("TERM", signal.SIGTERM, 2.0),
        ("KILL", signal.SIGKILL, 0.0),
    ):
        rows = scan_processes(owner_nonce, excluded=excluded, errors=errors) if owner_nonce else []
        for pid, pgid, _args in rows:
            if _send_pid(pid, sig_value):
                killed.append({"pid": pid, "pgid": pgid, "signal": sig_name})
        if delay:
            time.sleep(delay)
    residual = scan_processes(owner_nonce, excluded=excluded, errors=errors) if owner_nonce else []

    candidates, marker_errors = discover_owned_containers(
        base,
        runner_nonce=owner_nonce,
    )
    errors.extend(marker_errors)
    if owner is None:
        container_results = [
            {**candidate, "ok": False, "status": "runner_owner_unverified"} for candidate in candidates
        ]
    else:
        container_results = [remove_owned_container(candidate) for candidate in candidates]
    cleanup_ok = not errors and not residual and all(item.get("ok") is True for item in container_results)
    return {
        "ok": cleanup_ok,
        "status": "done" if cleanup_ok else "technical_cleanup_failed",
        "killed": killed,
        "containers": container_results,
        "scan_errors": errors,
        "residual_processes": [{"pid": pid, "pgid": pgid, "args": args[:500]} for pid, pgid, args in residual],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m opencollab.harness.swe_v1_remote_cleanup BASE_RUN_DIR", file=sys.stderr)
        return 2
    detail = cleanup_run(pathlib.Path(args[0]))
    print(json.dumps(detail, ensure_ascii=False))
    return 0 if detail["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [name for name in globals() if not name.startswith("__")]
