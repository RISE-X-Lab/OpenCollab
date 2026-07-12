"""Durable local state and committed prediction-output operations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from gen_prediction_constants import (
    HARNESS_LOCK_TIMEOUT_SECONDS,
    MAX_OUTPUT_JSONL_BYTES,
    SAFE_FILE_OPEN_RETRIES,
)
from opencollab.adapters._owned_file_cleanup import quarantine_unlink_owned_file
from opencollab.adapters.safe_files import (
    _open_directory_no_symlinks,
    create_regular_bytes_atomic,
    write_regular_bytes_atomic,
)
from opencollab.application.exception_notes import add_exception_note
from opencollab.harness.swe_generation_proof import current_generation_proof_valid


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting container ownership")
        view = view[written:]


def _cleanup_temporary_file(
    path: Path,
    original_error: BaseException | None,
    *,
    owned_fd: int | None = None,
) -> None:
    """Retire a temporary path only while its owned descriptor proves identity."""
    parent_fd = -1
    try:
        if owned_fd is None:
            raise OSError(
                f"refusing temporary-file cleanup without live ownership proof: {path}"
            )
        opened = os.fstat(owned_fd)
        parent_fd = _open_directory_no_symlinks(path.parent)
        quarantine_unlink_owned_file(
            parent_fd,
            path.name,
            owned_fd,
            (opened.st_dev, opened.st_ino),
            path_label=str(path),
        )
    except FileNotFoundError:
        return
    except BaseException as cleanup_error:
        if original_error is None:
            raise
        add_exception_note(
            original_error,
            "temporary-file retirement failed during cleanup: "
            f"{type(cleanup_error).__name__}: {cleanup_error}",
        )
        return
    finally:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except BaseException as cleanup_error:
                if original_error is None:
                    raise
                add_exception_note(
                    original_error,
                    "temporary-file parent fd close failed during cleanup: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                )


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_flags = flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"refusing non-regular harness file: {path}")
        try:
            if before is None:
                fd = os.open(path, safe_flags | os.O_CREAT | os.O_EXCL, mode)
                created = True
            else:
                fd = os.open(path, safe_flags)
                created = False
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise OSError(f"refusing non-regular harness file: {path}")
            opened_identity = (opened.st_dev, opened.st_ino)
            if (current.st_dev, current.st_ino) != opened_identity:
                continue
            if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                continue
            result_fd = fd
            fd = -1
            return result_fd, created
        except FileNotFoundError:
            pass
        finally:
            if fd >= 0:
                os.close(fd)
    raise OSError(f"harness file did not stabilize while opening: {path}")


def _path_matches_open_file(path: Path, fd: int) -> bool:
    """Return whether ``path`` still names the regular file held by ``fd``.

    The descriptor deliberately remains open while this comparison runs.  An
    unlinked inode therefore cannot be recycled into a same-number successor
    and defeat the identity check on filesystems with eager inode reuse.
    """
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return bool(
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _require_path_matches_open_file(path: Path, fd: int) -> None:
    if not _path_matches_open_file(path, fd):
        raise OSError(f"output path changed during durable append: {path}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    write_regular_bytes_atomic(path, payload)


def _atomic_create_bytes(path: Path, payload: bytes) -> None:
    """Atomically create ``path`` without replacing another live owner."""
    create_regular_bytes_atomic(path, payload)


def _atomic_write_text(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))


def _patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def runner_returncode_for_metrics(metrics: dict) -> int:
    if "runner_returncode" in metrics:
        existing = metrics["runner_returncode"]
        if isinstance(existing, bool) or not isinstance(existing, int):
            raise ValueError("runner_returncode must be a non-boolean integer")
        status = str(metrics.get("workflow_status") or "")
        expected = {"done": 0, "done_with_timeout_patch": 124}.get(status)
        if expected is not None and existing != expected:
            raise ValueError(f"runner_returncode {existing} conflicts with workflow_status {status!r}")
        return existing
    status = str(metrics.get("workflow_status") or "")
    if status == "done":
        return 0
    if status == "done_with_timeout_patch":
        return 124
    return 1


def metrics_have_completed_identity(metrics: dict, patch: str) -> bool:
    if not patch.strip():
        return False
    if metrics.get("execution_quiesced") is not True:
        return False
    if metrics.get("submission_eligible") is not True:
        return False
    if not current_generation_proof_valid(metrics, patch):
        return False
    try:
        returncode = runner_returncode_for_metrics(metrics)
    except ValueError:
        return False
    status = str(metrics.get("workflow_status") or "")
    if status == "done":
        return returncode == 0
    if status == "done_with_timeout_patch":
        return returncode == 124
    return False


def complete_single_agent_integrity(
    metrics: dict,
    *,
    patch: str,
    patch_extraction_succeeded: bool,
) -> None:
    """Record every proof field required by current harness records."""
    proof_valid = patch_extraction_succeeded and current_generation_proof_valid(
        metrics,
        patch,
    )
    metrics.update(
        {
            "patch_extraction_succeeded": proof_valid,
            "injected_path_cleanup_proven": proof_valid,
            "harness_artifact_exclusion_proven": proof_valid,
            "checkpoint_restore_integrity_proven": proof_valid,
            "task_stage_integrity_proven": proof_valid,
            "test_patch_isolation_failed": False,
            "worktree_integrity_proven": proof_valid,
        }
    )
    metrics["submission_eligible"] = (
        metrics.get("submission_eligible") is True and proof_valid
    )


def normalize_trusted_extraction_status(metrics: dict, patch: str) -> None:
    """Represent a proved zero-byte candidate without hiding timeout failures."""
    if patch.strip():
        return
    metrics["submission_eligible"] = False
    if metrics.get("workflow_status") == "done":
        metrics["workflow_status"] = "empty_patch_after_done"


def build_output_records(
    *,
    instance_id: str,
    model_name: str,
    patch: str,
    metrics: dict,
    record_id: str | None = None,
) -> tuple[dict, dict]:
    record_id = record_id or uuid.uuid4().hex
    patch_sha = _patch_sha256(patch)
    metric_record = {
        **metrics,
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_name_or_path": model_name,
    }
    metric_record["runner_returncode"] = runner_returncode_for_metrics(metric_record)
    prediction = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_name_or_path": model_name,
        "model_patch": patch,
        "workflow_metric": metric_record,
    }
    return prediction, metric_record


def default_metrics_path(output_path: Path) -> Path:
    return output_path.with_name("metrics.jsonl")


def output_paths(
    output: str | Path,
    metrics: str | Path | None,
) -> tuple[Path, Path]:
    predictions_path = Path(output)
    metrics_path = Path(metrics) if metrics else default_metrics_path(predictions_path)
    predictions_path = _validate_output_target(predictions_path)
    metrics_path = _validate_output_target(metrics_path)
    if output_paths_collide(predictions_path, metrics_path):
        raise ValueError("prediction and metric outputs must use different files")
    return predictions_path, metrics_path


def _validate_output_target(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        existing = absolute.lstat()
    except FileNotFoundError:
        return absolute
    if not stat.S_ISREG(existing.st_mode):
        raise ValueError(f"output path must be a regular file or absent: {path}")
    return absolute


def output_paths_collide(first: Path, second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError):
        return False


def _append_jsonl_durable(path: Path, row: dict) -> None:
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
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_output_records(
    predictions_path: Path,
    metrics_path: Path,
    prediction: dict,
    metric: dict,
) -> None:
    if output_paths_collide(predictions_path, metrics_path):
        raise ValueError("prediction and metric outputs must use different files")
    # The prediction is a self-contained commit record: its embedded metric is
    # enough for recovery if the external metrics projection cannot be written.
    _append_jsonl_durable(predictions_path, prediction)
    _append_jsonl_durable(metrics_path, metric)
