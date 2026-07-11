"""Safe inputs, claims, candidate records, and queue selection for SWE-bench eval."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
from pathlib import Path, PureWindowsPath

from opencollab.adapters.safe_files import (
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    regular_path_identity,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)
from opencollab.harness import swe_eval_records as swe_records
from opencollab.harness.swe_eval_records import (
    MAX_JSON_DOCUMENT_BYTES,
    SUBMISSION_INTEGRITY_PROVEN,
    RecordInputFormatError,
    RecordInputLimitError,
    UnsafeRecordInputError,
    embedded_workflow_metric,
    is_completed_prediction,
    metric_submission_integrity,
)


def _runner():
    module = sys.modules.get("scripts.run_swebench_eval_per_instance")
    if module is not None:
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("run_swebench_eval_per_instance.py"):
        return module
    raise RuntimeError("per-instance evaluator runner module is unavailable")


def positive_timeout_seconds(value: object, *, name: str) -> float:
    raw = str(value).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}")
    return timeout


def positive_int_arg(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("expected a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def nonnegative_int_arg(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return int(value)


def validate_path_identity(value: object, *, name: str) -> str:
    text = str(value)
    if not text:
        raise ValueError(f"{name} must not be empty")
    if text in {".", ".."}:
        raise ValueError(f"{name} must not be a dot path segment")
    if "/" in text or "\\" in text:
        raise ValueError(f"{name} must not contain path separators")
    windows_path = PureWindowsPath(text)
    if Path(text).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{name} must not be absolute or drive-qualified")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise ValueError(f"{name} must not contain control, format, or surrogate characters")
    encoded = text.encode("utf-8")
    if len(encoded) > _runner().MAX_PATH_IDENTITY_BYTES:
        raise ValueError(f"{name} exceeds {_runner().MAX_PATH_IDENTITY_BYTES} UTF-8 bytes")
    return text


def validate_model_identity(value: object) -> str:
    name = "model_name_or_path"
    text = str(value)
    if not text:
        raise ValueError(f"{name} must not be empty")
    if "\\" in text:
        raise ValueError(f"{name} must not contain backslash separators")
    windows_path = PureWindowsPath(text)
    if Path(text).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{name} must not be absolute or drive-qualified")
    segments = text.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} must not contain empty or dot path segments")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise ValueError(f"{name} must not contain control, format, or surrogate characters")
    encoded_component = text.replace("/", "__").encode("utf-8")
    if len(encoded_component) > _runner().MAX_PATH_IDENTITY_BYTES:
        raise ValueError(f"{name} exceeds {_runner().MAX_PATH_IDENTITY_BYTES} encoded UTF-8 bytes")
    return text


def read_jsonl(path: Path) -> list[dict]:
    try:
        payload = read_regular_bytes(
            path,
            max_bytes=swe_records.MAX_JSONL_SCAN_BYTES,
        )
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise RecordInputLimitError(f"JSONL input exceeds {swe_records.MAX_JSONL_SCAN_BYTES} bytes: {path}") from exc
    except OSError as exc:
        raise UnsafeRecordInputError(f"cannot safely read JSONL input: {path}") from exc
    rows: list[dict] = []
    retained_bytes = 0
    for line in payload.splitlines(keepends=True):
        if not line.strip():
            continue
        if len(line) > swe_records.MAX_JSONL_LINE_BYTES:
            raise RecordInputLimitError(f"JSONL line exceeds {swe_records.MAX_JSONL_LINE_BYTES} bytes: {path}")
        retained_bytes += len(line)
        if len(rows) >= swe_records.MAX_JSONL_RETAINED_ROWS or retained_bytes > swe_records.MAX_JSONL_RETAINED_BYTES:
            raise RecordInputLimitError(f"JSONL input exceeds retained row or byte limit: {path}")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordInputFormatError(f"invalid JSONL record in {path}") from exc
        if not isinstance(value, dict):
            raise RecordInputFormatError(f"JSONL record must be an object: {path}")
        rows.append(value)
    return rows


def read_dataset(path: Path) -> list[dict]:
    try:
        raw = read_regular_bytes(path, max_bytes=_runner().MAX_DATASET_BYTES)
    except ValueError as exc:
        raise ValueError(f"dataset exceeds {_runner().MAX_DATASET_BYTES} bytes: {path}") from exc
    except OSError as exc:
        raise ValueError(f"dataset is not a bounded regular file: {path}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        payload = None
    else:
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            if len(payload) > _runner().MAX_DATASET_ROWS:
                raise ValueError(f"dataset exceeds {_runner().MAX_DATASET_ROWS} rows")
            if not all(isinstance(row, dict) for row in payload):
                raise ValueError("dataset JSON list must contain only objects")
            return payload
        raise ValueError("dataset must be a JSON object, JSON list, or JSONL records")

    rows: list[dict] = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        if len(line) > _runner().MAX_DATASET_LINE_BYTES:
            raise ValueError(f"dataset line exceeds {_runner().MAX_DATASET_LINE_BYTES} bytes: {path}")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"dataset contains invalid JSONL: {path}") from exc
        if not isinstance(row, dict):
            raise ValueError("dataset JSONL must contain only objects")
        rows.append(row)
        if len(rows) > _runner().MAX_DATASET_ROWS:
            raise ValueError(f"dataset exceeds {_runner().MAX_DATASET_ROWS} rows")
    return rows


def report_path(work_dir: Path, run_id: str, model_name: str, instance_id: str) -> Path:
    run_id = _runner().validate_path_identity(run_id, name="run_id")
    model_name = _runner().validate_model_identity(model_name)
    instance_id = _runner().validate_path_identity(instance_id, name="instance_id")
    return work_dir / "logs" / "run_evaluation" / run_id / model_name.replace("/", "__") / instance_id / "report.json"


def prediction_identity(prediction: dict) -> dict:
    patch = str(prediction.get("model_patch") or "")
    return {
        "instance_id": str(prediction.get("instance_id") or ""),
        "record_id": str(prediction.get("record_id") or ""),
        "patch_sha256": hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest() if patch else "",
    }


def prediction_is_eval_eligible(prediction: dict) -> bool:
    patch = str(prediction.get("model_patch") or "")
    if not patch.strip():
        return False
    modern_keys = {
        "workflow_metric",
        "workflow_status",
        "runner_returncode",
        "patch_sha256",
        "patch_sha",
        "model_patch_sha256",
        "submission_eligible",
        "execution_quiesced",
        "patch_extraction_succeeded",
    }
    if modern_keys.intersection(prediction):
        metric = embedded_workflow_metric(prediction)
        return (
            is_completed_prediction(prediction) and metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_PROVEN
        )
    # Rows predating embedded workflow metrics remain evaluable when they are
    # complete plain prediction records.  Any modern provenance signal above
    # switches the row to the strict paired-integrity gate.
    return True


def identity_path(path: Path) -> Path:
    return path.with_name("opencollab-attempt.json")


def _read_bounded_json_safe(
    path: Path,
    *,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
) -> tuple[object, os.stat_result] | None:
    target = Path(os.path.abspath(os.fspath(path)))
    try:
        parent_fd = _open_directory_no_symlinks(target.parent)
    except OSError:
        return None
    fd = -1
    try:
        try:
            before = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(target.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            return None
        current = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )

        def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if identity(opened) != identity(after) or identity(after) != identity(current):
            return None
        if not _directory_path_matches_fd(target.parent, parent_fd):
            return None
        try:
            return json.loads(payload.decode("utf-8")), after
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def file_fingerprint(path: Path) -> str:
    try:
        _dev, inode, size, mtime_ns, ctime_ns = regular_path_identity(path)
    except OSError:
        return ""
    return f"{mtime_ns}:{ctime_ns}:{size}:{inode}"


def _stat_fingerprint(opened: os.stat_result) -> str:
    return f"{opened.st_mtime_ns}:{opened.st_ctime_ns}:{opened.st_size}:{opened.st_ino}"


def _fsync_directory(path: Path) -> None:
    fd = _open_directory_no_symlinks(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_exclusive_lock(fd: int, *, label: str) -> None:
    deadline = time.monotonic() + _runner().HARNESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring {label} after {_runner().HARNESS_LOCK_TIMEOUT_SECONDS:g}s")
        time.sleep(min(0.01, remaining))


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    path = Path(os.path.abspath(os.fspath(path)))
    ensure_directory_no_symlinks(path.parent)
    safe_flags = flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(_runner().SAFE_FILE_OPEN_RETRIES):
        parent_fd = _open_directory_no_symlinks(path.parent)
        try:
            try:
                before = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise OSError(f"refusing non-regular harness file: {path}")
            try:
                if before is None:
                    fd = os.open(
                        path.name,
                        safe_flags | os.O_CREAT | os.O_EXCL,
                        mode,
                        dir_fd=parent_fd,
                    )
                    created = True
                else:
                    fd = os.open(path.name, safe_flags, dir_fd=parent_fd)
                    created = False
            except (FileExistsError, FileNotFoundError):
                continue
            try:
                opened = os.fstat(fd)
                current = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                    raise OSError(f"refusing non-regular harness file: {path}")
                opened_identity = (opened.st_dev, opened.st_ino)
                if (current.st_dev, current.st_ino) != opened_identity:
                    continue
                if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                    continue
                if not _directory_path_matches_fd(path.parent, parent_fd):
                    break
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
        finally:
            os.close(parent_fd)
    raise OSError(f"harness file did not stabilize while opening: {path}")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    write_regular_bytes_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _runner()._write_bytes_atomic(path, encoded)


def _unlink_durable(path: Path) -> None:
    unlink_regular_file_durable(path)


def _open_append_text(path: Path):
    fd, _created = _runner()._open_regular_file(path, os.O_WRONLY | os.O_APPEND, 0o644)
    return os.fdopen(fd, "a", encoding="utf-8")


def write_identity(
    path: Path,
    identity: dict,
    *,
    status: str = "started",
    pid: int = 0,
    started_at_ns: int | None = None,
    prior_report_fingerprint: str | None = None,
) -> dict:
    if started_at_ns is None:
        started_at_ns = time.time_ns()
    if prior_report_fingerprint is None:
        prior_report_fingerprint = _runner().file_fingerprint(path.with_name("report.json"))
    payload = {
        "schema": "opencollab.swe_eval_attempt.v1",
        **identity,
        "started_at_ns": started_at_ns,
        "status": status,
        "pid": pid,
        "prior_report_fingerprint": prior_report_fingerprint,
    }
    _runner()._write_json_atomic(path, payload)
    return payload


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


def _claim_path(work_dir: Path, instance_id: str) -> Path:
    instance_id = _runner().validate_path_identity(instance_id, name="instance_id")
    stem = hashlib.sha256(instance_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return work_dir / ".opencollab" / "claims" / f"{stem}.json"


def acquire_claim(
    work_dir: Path,
    instance_id: str,
    identity: dict,
    *,
    lease_seconds: int,
    owner_token: str,
) -> tuple[bool, Path]:
    path = _runner()._claim_path(work_dir, instance_id)
    ensure_directory_no_symlinks(path.parent)
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _runner()._open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _runner()._acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _runner()._read_bounded_json_safe(path)
        existing = document[0] if document is not None else {}
        if isinstance(existing, dict):
            now_ns = time.time_ns()
            try:
                lease_until_ns = int(existing.get("lease_until_ns") or 0)
            except (TypeError, ValueError):
                lease_until_ns = 0
            residual_status = existing.get("status") in {
                "running",
                "cleanup_failed",
            }
            if residual_status and _runner()._claim_residual_group_is_live(existing):
                existing["lease_until_ns"] = now_ns + 60_000_000_000
                existing["residual_checked_at_ns"] = now_ns
                _runner()._write_json_atomic(path, existing)
                return False, path
            if existing.get("status") != "cleanup_failed" and lease_until_ns > now_ns:
                return False, path
        claimed_at_ns = time.time_ns()
        _runner()._write_json_atomic(
            path,
            {
                "schema": "opencollab.swe_eval_claim.v1",
                **identity,
                "owner_token": owner_token,
                "pid": os.getpid(),
                "status": "claimed",
                "claimed_at_ns": claimed_at_ns,
                "lease_until_ns": claimed_at_ns + max(1, lease_seconds) * 1_000_000_000,
            },
        )
        return True, path
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def update_claim_process(
    path: Path,
    *,
    owner_token: str,
    evaluator_pgid: int,
    evaluator_start_identity: str,
    status: str,
    lease_seconds: int,
) -> bool:
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _runner()._open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _runner()._acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _runner()._read_bounded_json_safe(path)
        if document is None:
            return False
        existing, _opened_stat = document
        if not isinstance(existing, dict) or existing.get("owner_token") != owner_token:
            return False
        now_ns = time.time_ns()
        existing.update(
            {
                "status": status,
                "evaluator_pgid": evaluator_pgid,
                "evaluator_start_identity": evaluator_start_identity,
                "updated_at_ns": now_ns,
                "lease_until_ns": now_ns + max(1, lease_seconds) * 1_000_000_000,
            }
        )
        _runner()._write_json_atomic(path, existing)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def release_claim(path: Path, *, owner_token: str) -> bool:
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _runner()._open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _runner()._acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _runner()._read_bounded_json_safe(path)
        if document is None:
            return False
        existing, _opened_stat = document
        if not isinstance(existing, dict) or existing.get("owner_token") != owner_token:
            return False
        _runner()._unlink_durable(path)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def candidate_predictions_path(work_dir: Path, identity: dict) -> Path:
    instance_id = _runner().validate_path_identity(
        identity.get("instance_id") or "",
        name="instance_id",
    )
    key = "\0".join(
        [
            instance_id,
            str(identity.get("record_id") or ""),
            str(identity.get("patch_sha256") or ""),
        ]
    )
    stem = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
    return work_dir / ".opencollab" / "candidates" / f"{stem}.jsonl"


def write_candidate_prediction(work_dir: Path, prediction: dict, identity: dict) -> Path:
    path = _runner().candidate_predictions_path(work_dir, identity)
    payload = (json.dumps(prediction, ensure_ascii=False) + "\n").encode("utf-8")
    _runner()._write_bytes_atomic(path, payload)
    return path


def report_is_done(path: Path, instance_id: str, expected_identity: dict) -> bool:
    document = _runner()._read_bounded_json_safe(path)
    if document is None:
        return False
    data, report_stat = document
    item = data.get(instance_id) if isinstance(data, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("resolved"), bool):
        return False
    if str(item.get("status") or "") in _runner().TECHNICAL_REPORT_STATUSES or bool(item.get("error")):
        return False
    embedded_sha = str(item.get("patch_sha256") or item.get("patch_sha") or item.get("model_patch_sha256") or "")
    if embedded_sha:
        return embedded_sha == expected_identity.get("patch_sha256")
    sidecar = _runner().identity_path(path)
    sidecar_document = _runner()._read_bounded_json_safe(sidecar)
    if sidecar_document is None:
        return False
    attempt, _sidecar_stat = sidecar_document
    report_mtime_ns = report_stat.st_mtime_ns
    if not isinstance(attempt, dict) or attempt.get("schema") != "opencollab.swe_eval_attempt.v1":
        return False
    if attempt.get("status") not in {"launching", "started", "completed"}:
        return False
    if str(attempt.get("instance_id") or "") != instance_id:
        return False
    if str(attempt.get("record_id") or "") != str(expected_identity.get("record_id") or ""):
        return False
    if str(attempt.get("patch_sha256") or "") != str(expected_identity.get("patch_sha256") or ""):
        return False
    if "prior_report_fingerprint" in attempt:
        # A report format without an embedded patch identity can only be bound
        # when the destination was absent at attempt start. Rewriting or merely
        # touching a pre-existing report does not prove which patch was graded.
        if str(attempt.get("prior_report_fingerprint") or ""):
            return False
    try:
        return report_mtime_ns >= int(attempt.get("started_at_ns") or 0) > 0
    except (TypeError, ValueError):
        return False


def load_eval_queue(
    dataset_path: Path,
    predictions_path: Path,
    run_id: str,
    work_dir: Path,
) -> list[tuple[str, str, dict, dict]]:
    run_id = _runner().validate_path_identity(run_id, name="run_id")
    dataset = _runner().read_dataset(dataset_path)
    predictions = {
        str(row["instance_id"]): row for row in _runner().read_jsonl(predictions_path) if row.get("instance_id")
    }
    queue: list[tuple[str, str, dict, dict]] = []
    for instance in dataset:
        iid = str(instance.get("instance_id") or "")
        if not iid:
            continue
        iid = _runner().validate_path_identity(iid, name="instance_id")
        prediction = predictions.get(iid)
        if not prediction:
            continue
        if not _runner().prediction_is_eval_eligible(prediction):
            continue
        model_name = str(prediction.get("model_name_or_path") or "unknown-model")
        model_name = _runner().validate_model_identity(model_name)
        identity = _runner().prediction_identity(prediction)
        if _runner().report_is_done(_runner().report_path(work_dir, run_id, model_name, iid), iid, identity):
            continue
        queue.append((iid, model_name, identity, prediction))
    return queue
