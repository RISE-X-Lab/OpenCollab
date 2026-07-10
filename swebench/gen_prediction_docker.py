"""Docker lifecycle and durable container-ownership records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

from gen_prediction_config import _docker_timeout_from_env
from gen_prediction_constants import (
    _MISSING_CONTAINER_RE,
    CONTAINER_OWNER_LABEL,
    CONTAINER_OWNER_SCHEMA_VERSION,
    DOCKER_WORKDIR,
    MAX_COMPATIBILITY_MARKER_BYTES,
    MAX_OWNER_RECORD_BYTES,
)
from gen_prediction_safe_output import (
    _atomic_create_bytes,
    _atomic_write_bytes,
    _atomic_write_text,
    _fsync_directory,
)
from opencollab.harness.swe_eval_records import open_regular_binary, read_bounded_json

from scripts.swebench_process import process_start_identity as _process_start_identity


def _read_small_regular_text(path: Path) -> str | None:
    try:
        with open_regular_binary(path) as handle:
            info = os.fstat(handle.fileno())
            if info.st_size > MAX_COMPATIBILITY_MARKER_BYTES:
                return None
            raw = handle.read(MAX_COMPATIBILITY_MARKER_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_COMPATIBILITY_MARKER_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def _docker(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = _docker_timeout_from_env()
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _check_docker(res: subprocess.CompletedProcess, action: str) -> None:
    if res.returncode == 0:
        return
    detail = (res.stderr or res.stdout).strip()
    raise RuntimeError(f"{action} failed (exit {res.returncode}): {detail}")


def _container_owner_label_state(reference: str, owner_token: str) -> str:
    try:
        result = _docker(
            "inspect",
            "--type",
            "container",
            "--format",
            f'{{{{ index .Config.Labels "{CONTAINER_OWNER_LABEL}" }}}}',
            reference,
            timeout=30,
        )
    except BaseException:
        return "unknown"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if _MISSING_CONTAINER_RE.search(detail) is not None:
            return "absent"
        return "unknown"
    label = result.stdout.strip()
    if not label or "\n" in label or "\r" in label:
        return "foreign"
    return "matching" if label == owner_token else "foreign"


def _remove_labeled_container(
    reference: str,
    owner_token: str,
    *,
    foreign_proves_absence: bool,
) -> bool:
    state = _container_owner_label_state(reference, owner_token)
    if state == "absent":
        return True
    if state == "foreign":
        return foreign_proves_absence
    if state != "matching":
        return False
    return remove_container(reference)


def _require_creation_cleanup(
    reference: str,
    owner_token: str,
    cause: BaseException,
    *,
    foreign_proves_absence: bool,
) -> None:
    if _remove_labeled_container(
        reference,
        owner_token,
        foreign_proves_absence=foreign_proves_absence,
    ):
        return
    raise RuntimeError(
        "container creation failed and owner-label cleanup could not be proven; no unverified container was removed"
    ) from cause


def start_container(
    image: str,
    name: str,
    owner_token: str | None = None,
) -> str:
    owner_token = owner_token or uuid.uuid4().hex
    if re.fullmatch(r"[0-9a-f]{32}", owner_token) is None:
        raise ValueError("container owner token must be 32 lowercase hex characters")
    try:
        res = _docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{CONTAINER_OWNER_LABEL}={owner_token}",
            "--network",
            "none",
            "--entrypoint",
            "",
            image,
            "tail",
            "-f",
            "/dev/null",
        )
    except BaseException as exc:
        _require_creation_cleanup(
            name,
            owner_token,
            exc,
            foreign_proves_absence=True,
        )
        raise
    if res.returncode != 0:
        error = RuntimeError(f"docker run failed: {res.stderr.strip()}")
        _require_creation_cleanup(
            name,
            owner_token,
            error,
            foreign_proves_absence=True,
        )
        raise error
    cid = res.stdout.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{12,64}", cid) is None:
        error = RuntimeError("docker run returned an invalid container id")
        _require_creation_cleanup(
            name,
            owner_token,
            error,
            foreign_proves_absence=True,
        )
        raise error
    try:
        ensure_workdir = _docker(
            "exec",
            cid,
            "bash",
            "-lc",
            """
set -e
if [ -e /testbed/.git ]; then
  exit 0
fi
if { [ -e /testbed ] || [ -L /testbed ]; } && [ ! -e /testbed/.git ]; then
  rm -rf /testbed
fi
if [ ! -e /testbed ]; then
  for d in /app /workspace /repo /src; do
    if [ -e "$d/.git" ]; then
      ln -s "$d" /testbed
      exit 0
    fi
  done
  found=$(find / -maxdepth 3 -name .git 2>/dev/null | head -1 || true)
  if [ -n "$found" ]; then
    ln -s "$(dirname "$found")" /testbed
    exit 0
  fi
fi
echo "unable to prepare /testbed: no repository checkout found" >&2
exit 2
""",
        )
        _check_docker(ensure_workdir, "docker /testbed workdir setup")
        # Repo is owned by root in the image; allow git to operate on it.
        safe_dir = _docker("exec", cid, "bash", "-lc", f"git config --global --add safe.directory {DOCKER_WORKDIR}")
        _check_docker(safe_dir, "docker git safe.directory setup")
    except BaseException as exc:
        _require_creation_cleanup(
            cid,
            owner_token,
            exc,
            foreign_proves_absence=False,
        )
        raise
    return cid


def _container_is_absent(reference: str) -> bool:
    try:
        result = _docker("inspect", "--type", "container", reference, timeout=30)
    except Exception as exc:  # noqa: BLE001 - absence must be positively verified
        print(f"  warning: container absence check failed for {reference}: {exc!r}")
        return False
    detail = (result.stderr or result.stdout or "").strip()
    return result.returncode != 0 and _MISSING_CONTAINER_RE.search(detail) is not None


def remove_container(reference: str) -> bool:
    """Remove a container and return only after Docker proves it is absent."""
    try:
        result = _docker("rm", "-f", reference, timeout=30)
    except Exception as exc:  # noqa: BLE001 - retain ownership on unknown teardown
        print(f"  warning: container cleanup failed for {reference}: {exc!r}")
        return False
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0 and _MISSING_CONTAINER_RE.search(detail) is None:
        print(f"  warning: container cleanup failed for {reference}: exit {result.returncode}: {detail[:500]}")
        return False
    if not _container_is_absent(reference):
        print(f"  warning: Docker did not prove container {reference} absent after rm")
        return False
    return True


def _owner_directory(run_dir: Path) -> Path:
    return run_dir / ".opencollab" / "container_owners"


def container_owner_path(run_dir: Path, name: str) -> Path:
    digest = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _owner_directory(run_dir) / f"{digest}.json"


def _owner_record(name: str, *, state: str, cid: str = "") -> dict:
    return {
        "schema_version": CONTAINER_OWNER_SCHEMA_VERSION,
        "state": state,
        "container_name": name,
        "container_id": cid,
        "owner_pid": os.getpid(),
        "owner_start_identity": _process_start_identity(os.getpid()),
        "owner_token": uuid.uuid4().hex,
    }


def _encode_owner(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _read_owner(path: Path) -> dict | None:
    document = read_bounded_json(path, max_bytes=1024 * 1024)
    if document is None:
        return None
    value, _opened_stat = document
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != CONTAINER_OWNER_SCHEMA_VERSION:
        return None
    if value.get("state") not in {
        "pending",
        "active",
        "preservation_required",
        "candidate_staged",
        "kept",
    }:
        return None
    if not isinstance(value.get("container_name"), str) or not value["container_name"]:
        return None
    if not isinstance(value.get("container_id", ""), str):
        return None
    pid = value.get("owner_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(value.get("owner_start_identity", ""), str):
        return None
    if not isinstance(value.get("owner_token"), str) or not value["owner_token"]:
        return None
    return value


def _owner_is_live(record: dict) -> bool:
    pid = record["owner_pid"]
    expected = record.get("owner_start_identity", "")
    current = _process_start_identity(pid)
    if expected and current:
        return current == expected
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _create_pending_owner(run_dir: Path, name: str) -> dict:
    record = _owner_record(name, state="pending")
    _atomic_create_bytes(container_owner_path(run_dir, name), _encode_owner(record))
    return record


def _replace_owner(path: Path, previous: dict, updated: dict) -> None:
    current = _read_owner(path)
    if current is None or current.get("owner_token") != previous.get("owner_token"):
        raise RuntimeError("container ownership changed while updating marker")
    if current != previous:
        if current == updated:
            return
        raise RuntimeError("container ownership state changed while updating marker")
    _atomic_write_bytes(path, _encode_owner(updated))


def _path_matches_open_file(path: Path, fd: int) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return bool(stat.S_ISREG(current.st_mode) and current.st_dev == opened.st_dev and current.st_ino == opened.st_ino)


def _unlink_owner(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    try:
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > MAX_OWNER_RECORD_BYTES:
                raise OSError(f"container owner record exceeds byte limit: {path}")
            payload = handle.read(MAX_OWNER_RECORD_BYTES + 1)
            if not _path_matches_open_file(path, handle.fileno()):
                return
            path.unlink()
    except OSError:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        raise
    if len(payload) > MAX_OWNER_RECORD_BYTES:
        raise OSError(f"container owner record exceeds byte limit: {path}")
    try:
        _fsync_directory(path.parent)
    except BaseException:
        if not path.exists():
            try:
                _atomic_create_bytes(path, payload)
            except BaseException:
                pass
        raise


def _write_compatibility_markers(run_dir: Path, cid: str, name: str) -> None:
    marker_dir = run_dir / ".opencollab" / "containers" / cid
    _atomic_write_text(marker_dir / "container.id", cid + "\n")
    _atomic_write_text(marker_dir / "container.name", name + "\n")
    _atomic_write_text(run_dir / "container.id", cid + "\n")
    _atomic_write_text(run_dir / "container.name", name + "\n")


def write_container_marker(run_dir: Path, cid: str, name: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = container_owner_path(run_dir, name)
    previous = _read_owner(path)
    if previous is None:
        previous = _owner_record(name, state="pending")
        _atomic_create_bytes(path, _encode_owner(previous))
    if previous["container_name"] != name:
        raise RuntimeError("container owner marker name mismatch")
    updated = {**previous, "state": "active", "container_id": cid}
    _replace_owner(path, previous, updated)
    _write_compatibility_markers(run_dir, cid, name)


def _remove_owned_container(record: dict) -> bool:
    reference = record.get("container_id") or record["container_name"]
    return _remove_labeled_container(
        reference,
        record["owner_token"],
        foreign_proves_absence=not bool(record.get("container_id")),
    )


def recover_stale_container_owners(run_dir: Path) -> bool:
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return True
    recovered = True
    for path in sorted(owner_dir.glob("*.json")):
        record = _read_owner(path)
        if record is None:
            print(f"  warning: invalid container owner record retained: {path}")
            recovered = False
            continue
        if record["state"] == "preservation_required":
            print(f"  warning: container retained because output staging did not complete: {record['container_name']}")
            recovered = False
            continue
        if record["state"] == "kept" or _owner_is_live(record):
            continue
        if not _remove_owned_container(record):
            recovered = False
            continue
        _clear_compatibility_markers(run_dir, record.get("container_id") or None, record["container_name"])
        _unlink_owner(path)
    return recovered


def start_container_with_marker(
    image: str,
    name: str,
    run_dir: Path,
) -> str:
    """Persist ownership before Docker creation, then upgrade it with the CID."""
    from gen_prediction_pending import recover_generation_state

    if not recover_generation_state(run_dir):
        raise RuntimeError("stale generation state recovery failed")
    pending = _create_pending_owner(run_dir, name)
    try:
        cid = start_container(image, name, pending["owner_token"])
        write_container_marker(run_dir, cid, name)
    except BaseException:
        current = _read_owner(container_owner_path(run_dir, name)) or pending
        if _remove_owned_container(current):
            _clear_compatibility_markers(run_dir, current.get("container_id") or None, name)
            _unlink_owner(container_owner_path(run_dir, name))
        raise
    return cid


def _clear_compatibility_markers(
    run_dir: Path,
    cid: str | None = None,
    name: str | None = None,
) -> None:
    if cid:
        marker_dir = run_dir / ".opencollab" / "containers" / cid
        try:
            marker_fd = os.open(
                marker_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            marker_fd = -1
        if marker_fd >= 0:
            try:
                removed_marker = False
                for marker in ("container.id", "container.name"):
                    try:
                        (marker_dir / marker).unlink()
                        removed_marker = True
                    except FileNotFoundError:
                        pass
                if removed_marker:
                    os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            try:
                marker_dir.rmdir()
            except FileNotFoundError:
                _fsync_directory(marker_dir.parent)
            except OSError:
                pass
            else:
                _fsync_directory(marker_dir.parent)
    legacy_id = run_dir / "container.id"
    if cid:
        value = _read_small_regular_text(legacy_id)
        if value is None or value.strip() != cid:
            return
    elif name:
        value = _read_small_regular_text(run_dir / "container.name")
        if value is None or value.strip() != name:
            return
    removed_legacy = False
    for marker in (legacy_id, run_dir / "container.name"):
        try:
            marker.unlink()
            removed_legacy = True
        except FileNotFoundError:
            pass
    if removed_legacy:
        _fsync_directory(run_dir)


def clear_container_marker(
    run_dir: Path,
    cid: str | None = None,
    name: str | None = None,
) -> None:
    """Clear markers after the caller has already proved container absence."""
    _clear_compatibility_markers(run_dir, cid, name)
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return
    for path in owner_dir.glob("*.json"):
        record = _read_owner(path)
        if record is None:
            continue
        if (cid and record.get("container_id") == cid) or (name and record.get("container_name") == name):
            _unlink_owner(path)


def mark_container_kept(run_dir: Path, cid: str) -> None:
    for path in _owner_directory(run_dir).glob("*.json"):
        record = _read_owner(path)
        if record is None or record.get("container_id") != cid:
            continue
        _replace_owner(path, record, {**record, "state": "kept"})
        return
    raise RuntimeError(f"container ownership marker missing for kept container {cid}")


def remove_container_and_clear_marker(run_dir: Path, cid: str) -> bool:
    record = None
    for path in _owner_directory(run_dir).glob("*.json"):
        candidate = _read_owner(path)
        if candidate is not None and candidate.get("container_id") == cid:
            record = candidate
            break
    if record is None:
        return False
    if not _remove_owned_container(record):
        return False
    clear_container_marker(run_dir, cid, record["container_name"])
    return True


def finalize_container_ownership(
    *,
    run_dir: Path,
    cid: str,
    name: str,
    keep_container: bool,
    completed: bool,
    metrics: dict,
) -> None:
    if keep_container and completed:
        try:
            mark_container_kept(run_dir, cid)
        except BaseException as exc:
            if not remove_container_and_clear_marker(run_dir, cid):
                raise RuntimeError(
                    f"technical container cleanup failed for {cid} after keep-marker failure; ownership marker retained"
                ) from exc
            raise
        metrics["container_retained"] = True
        print(f"  (left container {cid} running: {name})")
        return
    if not remove_container_and_clear_marker(run_dir, cid):
        raise RuntimeError(f"technical container cleanup failed for {cid}; ownership marker retained")
    metrics["container_cleanup_succeeded"] = True
