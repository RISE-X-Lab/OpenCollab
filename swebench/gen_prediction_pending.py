"""Pending-output staging, publication, and crash recovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from gen_prediction_constants import (
    MAX_JSONL_SCAN_LINE_BYTES,
    MAX_OUTPUT_JSONL_BYTES,
    MAX_PENDING_OUTPUT_BYTES,
    PENDING_OUTPUT_SCHEMA_VERSION,
)
from gen_prediction_docker import (
    _encode_owner,
    _owner_directory,
    _read_owner,
    _replace_owner,
    container_owner_path,
    recover_stale_container_owners,
)
from gen_prediction_safe_output import (
    _acquire_exclusive_lock,
    _atomic_create_bytes,
    _fsync_directory,
    _open_regular_file,
    _patch_sha256,
    _require_path_matches_open_file,
    _validate_output_target,
    _write_all,
    output_paths_collide,
)


def _pending_output_directory(run_dir: Path) -> Path:
    return run_dir / ".opencollab" / "pending_outputs"


def pending_output_path(run_dir: Path, instance_id: str, record_id: str) -> Path:
    identity = f"{instance_id}\0{record_id}"
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _pending_output_directory(run_dir) / f"{digest}.json"


def _row_output_identity(row: dict) -> tuple[str, str, str]:
    instance_id = str(row.get("instance_id") or "")
    record_id = str(row.get("record_id") or "")
    patch_sha = str(row.get("patch_sha256") or "")
    return instance_id, record_id, patch_sha


def _validate_pending_candidate(candidate: dict) -> None:
    if candidate.get("schema_version") != PENDING_OUTPUT_SCHEMA_VERSION:
        raise ValueError("unsupported pending output schema")
    prediction = candidate.get("prediction")
    metric = candidate.get("metric")
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        raise ValueError("pending output must contain prediction and metric objects")
    prediction_identity = _row_output_identity(prediction)
    metric_identity = _row_output_identity(metric)
    if not prediction_identity[0] or not prediction_identity[1]:
        raise ValueError("pending output identity is incomplete")
    if prediction_identity != metric_identity:
        raise ValueError("pending prediction and metric identities differ")
    patch = str(prediction.get("model_patch") or "")
    computed_sha = _patch_sha256(patch)
    if not computed_sha or prediction_identity[2] != computed_sha:
        raise ValueError("pending prediction patch SHA is invalid")
    embedded = prediction.get("workflow_metric")
    if not isinstance(embedded, dict) or embedded != metric:
        raise ValueError("pending embedded metric differs from external metric")
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ValueError("pending metric runner_returncode is invalid")
    for key in (
        "predictions_path",
        "metrics_path",
        "container_id",
        "container_name",
        "owner_token",
    ):
        if not isinstance(candidate.get(key), str) or not candidate[key]:
            raise ValueError(f"pending output field {key} is missing")
    if not Path(candidate["predictions_path"]).is_absolute() or not Path(candidate["metrics_path"]).is_absolute():
        raise ValueError("pending output targets must be absolute paths")
    _validate_output_target(Path(candidate["predictions_path"]))
    _validate_output_target(Path(candidate["metrics_path"]))
    if output_paths_collide(Path(candidate["predictions_path"]), Path(candidate["metrics_path"])):
        raise ValueError("pending output targets collide")


def persist_pending_output(
    *,
    run_dir: Path,
    predictions_path: Path,
    metrics_path: Path,
    prediction: dict,
    metric: dict,
    cid: str,
    name: str,
) -> Path:
    absolute_predictions = _validate_output_target(predictions_path)
    absolute_metrics = _validate_output_target(metrics_path)
    if output_paths_collide(absolute_predictions, absolute_metrics):
        raise ValueError("pending output targets collide")
    owner_path = container_owner_path(run_dir, name)
    owner = _read_owner(owner_path)
    if owner is None or owner.get("container_id") != cid or owner.get("state") != "active":
        raise RuntimeError("active container ownership is missing before output staging")
    preserving_owner = {**owner, "state": "preservation_required"}
    _replace_owner(owner_path, owner, preserving_owner)
    candidate = {
        "schema_version": PENDING_OUTPUT_SCHEMA_VERSION,
        "container_id": cid,
        "container_name": name,
        "owner_token": owner["owner_token"],
        "predictions_path": str(absolute_predictions),
        "metrics_path": str(absolute_metrics),
        "prediction": prediction,
        "metric": metric,
    }
    _validate_pending_candidate(candidate)
    instance_id, record_id, _patch_sha = _row_output_identity(prediction)
    path = pending_output_path(run_dir, instance_id, record_id)
    payload = _encode_owner(candidate)
    if len(payload) > MAX_PENDING_OUTPUT_BYTES:
        raise ValueError("pending output exceeds its byte limit")
    _atomic_create_bytes(path, payload)
    _replace_owner(
        owner_path,
        preserving_owner,
        {**preserving_owner, "state": "candidate_staged"},
    )
    return path


def output_staging_requires_container_preservation(
    run_dir: Path,
    *,
    cid: str,
    name: str,
) -> bool:
    owner = _read_owner(container_owner_path(run_dir, name))
    return bool(
        owner is not None
        and owner.get("container_id") == cid
        and owner.get("container_name") == name
        and owner.get("state") in {"preservation_required", "candidate_staged", "kept"}
    )


def _read_pending_fd(fd: int) -> tuple[dict, bytes]:
    size = os.fstat(fd).st_size
    if size <= 0 or size > MAX_PENDING_OUTPUT_BYTES:
        raise ValueError("pending output size is invalid")
    payload = os.pread(fd, size, 0)
    if len(payload) != size:
        raise OSError("short read while loading pending output")
    try:
        candidate = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pending output JSON is invalid") from exc
    if not isinstance(candidate, dict):
        raise ValueError("pending output JSON must be an object")
    _validate_pending_candidate(candidate)
    return candidate, payload


def _open_pending_regular(path: Path) -> int:
    fd = os.open(
        path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("pending output path is not a regular file")
        if opened.st_size <= 0 or opened.st_size > MAX_PENDING_OUTPUT_BYTES:
            raise ValueError("pending output size is invalid")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _candidate_matches_owner(candidate: dict, owner: dict) -> bool:
    return (
        candidate.get("container_id") == owner.get("container_id")
        and candidate.get("container_name") == owner.get("container_name")
        and candidate.get("owner_token") == owner.get("owner_token")
    )


def _preservation_was_superseded(owner_path: Path, owner: dict) -> bool:
    current = _read_owner(owner_path)
    if current is None:
        return not owner_path.exists()
    return current.get("owner_token") == owner.get("owner_token") and current.get("state") in {
        "candidate_staged",
        "kept",
    }


def _promote_durable_preservation_candidates(run_dir: Path) -> bool:
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return True
    pending_dir = _pending_output_directory(run_dir)
    promoted = True
    for owner_path in sorted(owner_dir.glob("*.json")):
        owner = _read_owner(owner_path)
        if owner is None or owner.get("state") != "preservation_required":
            continue
        matching_paths: list[Path] = []
        if pending_dir.exists():
            for path in sorted(pending_dir.glob("*.json")):
                try:
                    fd = _open_pending_regular(path)
                    locked = False
                    try:
                        _acquire_exclusive_lock(
                            fd,
                            label=f"pending-output lock {path}",
                        )
                        locked = True
                        candidate, _payload = _read_pending_fd(fd)
                    finally:
                        try:
                            if locked:
                                fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)
                except BaseException:
                    continue
                if _candidate_matches_owner(candidate, owner):
                    matching_paths.append(path)
        if len(matching_paths) != 1:
            if not _preservation_was_superseded(owner_path, owner):
                promoted = False
            continue
        path = matching_paths[0]
        try:
            fd = _open_pending_regular(path)
            locked = False
            try:
                _acquire_exclusive_lock(
                    fd,
                    label=f"pending-output lock {path}",
                )
                locked = True
                candidate, _payload = _read_pending_fd(fd)
                if not _candidate_matches_owner(candidate, owner):
                    raise RuntimeError("pending output identity changed during preservation recovery")
                os.fsync(fd)
                _fsync_directory(path.parent)
                _replace_owner(
                    owner_path,
                    owner,
                    {**owner, "state": "candidate_staged"},
                )
            finally:
                try:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        except BaseException as exc:
            if _preservation_was_superseded(owner_path, owner):
                continue
            print(f"  warning: preserved candidate validation failed for {owner['container_name']}: {exc!r}")
            promoted = False
    return promoted


def _find_committed_identity(fd: int, expected: dict) -> bool:
    expected_instance, expected_record, expected_sha = _row_output_identity(expected)
    size = os.fstat(fd).st_size
    if size > MAX_OUTPUT_JSONL_BYTES:
        raise OSError("output JSONL exceeds byte limit")
    data = os.pread(fd, size, 0)
    if len(data) != size:
        raise OSError("output JSONL changed during validation")
    lines = data.splitlines(keepends=True)
    found = False
    tail_start = size
    if data and not data.endswith(b"\n"):
        tail_start = data.rfind(b"\n") + 1

    for index, line in enumerate(lines):
        terminated = line.endswith(b"\n")
        content = line[:-1] if terminated else line
        if len(line) > MAX_JSONL_SCAN_LINE_BYTES:
            raise OSError("output JSONL line exceeds byte limit")
        if not content.strip():
            raise OSError("output JSONL contains a blank record")
        try:
            row = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not terminated and index == len(lines) - 1:
                os.ftruncate(fd, tail_start)
                os.fsync(fd)
                break
            raise OSError("output JSONL contains a malformed record") from exc
        if not isinstance(row, dict):
            raise OSError("output JSONL record must be an object")
        instance_id, record_id, patch_sha = _row_output_identity(row)
        if instance_id != expected_instance or record_id != expected_record:
            continue
        if patch_sha != expected_sha or row != expected:
            raise RuntimeError("committed output conflicts with pending record identity")
        found = True
    return found


def _append_jsonl_durable_once(path: Path, row: dict) -> bool:
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_JSONL_BYTES:
        raise OSError(f"output JSONL row exceeds byte limit: {path}")
    fd, _created = _open_regular_file(
        path,
        os.O_RDWR | os.O_APPEND,
        0o644,
    )
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"output lock {path}")
        locked = True
        _require_path_matches_open_file(path, fd)
        if _find_committed_identity(fd, row):
            _fsync_directory(path.parent)
            _require_path_matches_open_file(path, fd)
            return False
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_OUTPUT_JSONL_BYTES:
            raise OSError(f"output JSONL exceeds byte limit: {path}")
        if needs_separator:
            _write_all(fd, b"\n")
        _write_all(fd, payload)
        os.fsync(fd)
        _fsync_directory(path.parent)
        _require_path_matches_open_file(path, fd)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _pending_owner_state(run_dir: Path, candidate: dict) -> str:
    owner_path = container_owner_path(run_dir, candidate["container_name"])
    if not owner_path.exists():
        return "absent"
    owner = _read_owner(owner_path)
    if owner is None:
        raise RuntimeError("pending output has an invalid container owner")
    if owner.get("owner_token") != candidate["owner_token"]:
        raise RuntimeError("pending output owner token mismatch")
    if owner["state"] == "kept":
        return "kept"
    return "deferred"


def _unlink_pending_locked(path: Path, fd: int, payload: bytes) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode):
        return
    if current.st_dev != os.fstat(fd).st_dev or current.st_ino != os.fstat(fd).st_ino:
        return
    path.unlink()
    try:
        _fsync_directory(path.parent)
    except BaseException:
        if not path.exists():
            try:
                _atomic_create_bytes(path, payload)
            except BaseException:
                pass
        raise


def publish_pending_output(run_dir: Path, path: Path) -> str:
    try:
        fd = _open_pending_regular(path)
    except FileNotFoundError:
        return "missing"
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"pending-output lock {path}")
        locked = True
        try:
            current = path.lstat()
        except FileNotFoundError:
            return "missing"
        opened = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            return "missing"
        candidate, payload = _read_pending_fd(fd)
        if _pending_owner_state(run_dir, candidate) == "deferred":
            return "deferred"
        _append_jsonl_durable_once(Path(candidate["predictions_path"]), candidate["prediction"])
        _append_jsonl_durable_once(Path(candidate["metrics_path"]), candidate["metric"])
        _unlink_pending_locked(path, fd, payload)
        return "published"
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def recover_generation_state(run_dir: Path) -> bool:
    candidates_promoted = _promote_durable_preservation_candidates(run_dir)
    owners_recovered = recover_stale_container_owners(run_dir)
    outputs_recovered = True
    pending_dir = _pending_output_directory(run_dir)
    if pending_dir.exists():
        for path in sorted(pending_dir.glob("*.json")):
            try:
                publish_pending_output(run_dir, path)
            except BaseException as exc:
                print(f"  warning: pending output recovery failed for {path}: {exc!r}")
                outputs_recovered = False
    return candidates_promoted and owners_recovered and outputs_recovered
