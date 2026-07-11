#!/usr/bin/env python3
"""Validated input and durable prediction I/O for ``start_team_run.sh``."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

from opencollab.adapters import _atomic_rename
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.adapters.retirement_registry import (
    registered_retirement_paths,
)
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    read_regular_bytes,
    regular_path_identity,
    unlink_regular_file_durable,
)

try:
    from scripts import swe_team_batch_io as batch_io
except ImportError:  # Direct execution places this module's directory on sys.path.
    import swe_team_batch_io as batch_io

MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_TASK_PROMPT_BYTES = 4 * 1024 * 1024
MAX_PENDING_RECORD_BYTES = 64 * 1024 * 1024
MAX_PENDING_PATCH_BYTES = 9 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = 256 * 1024 * 1024
REQUIRED_TRUE_INTEGRITY_FIELDS = (
    "submission_eligible",
    "execution_quiesced",
    "patch_extraction_succeeded",
    "injected_path_cleanup_proven",
    "harness_artifact_exclusion_proven",
    "checkpoint_restore_integrity_proven",
    "task_stage_integrity_proven",
)
REQUIRED_FALSE_INTEGRITY_FIELDS = ("test_patch_isolation_failed",)


def validate_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("--timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be a finite positive number")
    return timeout


def prepare_real_directory(candidate: str, containment_root: str = "") -> Path:
    absolute = batch_io.lexical_absolute(candidate)
    containment = (
        batch_io.lexical_absolute(containment_root) if containment_root else None
    )
    if containment is not None:
        try:
            contained = os.path.commonpath((absolute, containment)) == str(containment)
        except ValueError:
            contained = False
        if not contained or absolute == containment:
            raise ValueError("directory path escapes its required host root")
    _opened, directory_fd = batch_io.open_directory(absolute, create=True)
    os.close(directory_fd)
    return absolute


def validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows_path = PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or "/" in value
        or "\\" in value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise ValueError("instance_id must be one safe path component")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_INSTANCE_ID_BYTES:
        raise ValueError("instance_id exceeds its UTF-8 byte limit")
    return value


def _read_regular(path: Path, limit: int, *, label: str) -> bytes:
    try:
        return read_regular_bytes(path, max_bytes=limit)
    except (OSError, ValueError) as exc:
        message = str(exc)
        if "changed while reading" in message:
            raise OSError(f"{label} changed while reading") from exc
        raise OSError(f"{label} must be a bounded regular file") from exc


def _read_regular_with_identity(
    path: Path,
    limit: int,
    *,
    label: str,
) -> tuple[bytes, tuple[int, int]]:
    before = regular_path_identity(path)
    payload = _read_regular(path, limit, label=label)
    after = regular_path_identity(path)
    if before != after:
        raise OSError(f"{label} changed while reading")
    return payload, (before[0], before[1])


def read_instance(path: Path) -> dict[str, object]:
    raw = _read_regular(path, MAX_INSTANCE_BYTES, label="instance file")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("instance file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("instance file must contain one JSON object")
    return value


def _write_task(path: Path, payload: bytes) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise OSError("task prompt target must be a regular file") from exc
    if not stat.S_ISREG(before.st_mode):
        raise OSError("task prompt target must be a regular file")
    absolute, parent_fd, name = batch_io.open_parent(path, create=False)
    fd = -1
    try:
        try:
            fd, created = batch_io.open_regular_at(
                parent_fd, name, os.O_WRONLY, stat.S_IMODE(before.st_mode), label=absolute
            )
        except OSError as exc:
            raise OSError("task prompt target changed while writing") from exc
        opened = os.fstat(fd)
        if created or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("task prompt target changed while writing")
        os.ftruncate(fd, 0)
        batch_io.write_all(fd, payload)
        os.fsync(fd)
        written = os.fstat(fd)
        current = batch_io.stat_at(parent_fd, name)
        if (
            current is None
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or written.st_size != len(payload)
            or current.st_size != len(payload)
        ):
            raise OSError("task prompt target changed while writing")
        os.fsync(parent_fd)
        if not _parent_matches(absolute, parent_fd):
            raise OSError("task prompt parent changed while writing")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def prepare_task(instance_file: Path, task_file: Path, *, include_hints: bool) -> str:
    instance = read_instance(instance_file)
    instance_id = validate_instance_id(instance.get("instance_id"))
    repo_name = instance.get("repo") or ""
    problem_statement = instance.get("problem_statement") or ""
    hints = (instance.get("hints_text") or "") if include_hints else ""
    hints_text = str(hints).strip()
    fail_to_pass = instance.get("FAIL_TO_PASS") or []
    if isinstance(fail_to_pass, str):
        try:
            fail_to_pass = json.loads(fail_to_pass)
        except json.JSONDecodeError:
            fail_to_pass = []
    if not isinstance(fail_to_pass, list):
        fail_to_pass = []

    lines = [f"# Issue to fix in `{repo_name}`", "", str(problem_statement), ""]
    if hints_text:
        lines.extend(
            [
                "## Maintainer hints from the issue thread",
                "",
                "These are real comments from project maintainers / triagers on the",
                "upstream issue. They often name the exact file or class to change.",
                "Read them carefully BEFORE searching the codebase.",
                "",
                hints_text,
                "",
            ]
        )
    lines.append("## Tests that must pass after your fix")
    if fail_to_pass:
        lines.extend(f"- {test_name}" for test_name in fail_to_pass)
    else:
        lines.append("- (project test suite)")
    lines.extend(
        [
            "",
            "Note: a FAIL_TO_PASS test that doesn't exist in the repo yet is normal — ",
            "the graders add it as part of the test patch. Do NOT spend time grepping ",
            "for the test definition; focus on the source fix.",
            "",
            "Locate the root cause in the source, apply a minimal fix, and ensure "
            "the behavior described above is satisfied.",
            "",
        ]
    )
    payload = "\n".join(lines).encode("utf-8")
    if len(payload) > MAX_TASK_PROMPT_BYTES:
        raise ValueError("task prompt exceeds the 4 MiB CLI input bound")
    _write_task(task_file, payload)
    return instance_id


def validate_image(value: str) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > 512
        or value.startswith("-")
        or "://" in value
        or any(character.isspace() for character in value)
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", value) is None
    ):
        raise ValueError("image must be a bounded Docker reference, not an option")
    return value


def session_key(session_root: str, instance_id: str) -> str:
    return hashlib.sha256(
        (session_root + "\0" + instance_id).encode("utf-8")
    ).hexdigest()


def instance_digest(instance_id: str) -> str:
    return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:16]


def _atomic_create(path: Path, payload: bytes, limit: int) -> None:
    try:
        create_regular_bytes_atomic(path, payload, max_bytes=limit, mode=0o600)
    except FileExistsError as exc:
        raise OSError(f"pending artifact already exists: {path}") from exc
    except ValueError as exc:
        raise OSError(f"pending artifact exceeds {limit} bytes: {path}") from exc


def create_pending_prediction(
    record_path: Path,
    pending_patch_path: Path,
    source_patch_path: Path,
    output_path: Path,
    instance_id: str,
    model_name: str,
    returncode: int,
) -> None:
    patch_bytes, source_identity = _read_regular_with_identity(
        source_patch_path,
        MAX_PENDING_PATCH_BYTES,
        label="extracted patch",
    )
    try:
        patch = patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("extracted patch is not valid UTF-8") from exc
    patch_sha = hashlib.sha256(
        patch.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    record_id = uuid.uuid4().hex
    workflow_status = (
        "error"
        if returncode not in {0, 124}
        else "empty_patch"
        if not patch
        else "done"
        if returncode == 0
        else "done_with_timeout_patch"
    )
    metric = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "workflow_status": workflow_status,
        "runner_returncode": returncode,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": bool(patch.strip()),
    }
    record = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_name_or_path": model_name,
        "model_patch": patch,
        "workflow_metric": metric,
    }
    envelope = {
        "schema": "opencollab.pending-prediction.v1",
        "output_path": os.path.abspath(output_path),
        "record": record,
    }
    record_payload = (json.dumps(envelope, ensure_ascii=False) + "\n").encode()
    _atomic_create(record_path, record_payload, MAX_PENDING_RECORD_BYTES)
    source_parent_fd = pending_parent_fd = source_fd = -1
    try:
        source_absolute, source_parent_fd, source_name = batch_io.open_parent(source_patch_path, create=False)
        pending_absolute, pending_parent_fd, pending_name = batch_io.open_parent(pending_patch_path, create=False)
        source_parent, pending_parent = os.fstat(source_parent_fd), os.fstat(pending_parent_fd)
        if (source_parent.st_dev, source_parent.st_ino) != (pending_parent.st_dev, pending_parent.st_ino):
            raise OSError("extracted and pending patches must share one directory")
        source_fd, created = batch_io.open_regular_at(
            source_parent_fd, source_name, os.O_RDWR, 0o600, label=source_absolute
        )
        if created or (os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino) != source_identity:
            raise OSError("extracted patch changed before pending commit")
        os.fsync(source_fd)
        with os.fdopen(os.dup(source_fd), "rb", closefd=True) as source_handle:
            verified_patch = source_handle.read(MAX_PENDING_PATCH_BYTES + 1)
        if verified_patch != patch_bytes:
            raise OSError("extracted patch content changed before pending commit")
        _atomic_rename.rename_noreplace(
            source_name,
            pending_name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=source_parent_fd,
        )
        pending = os.stat(pending_name, dir_fd=source_parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(pending.st_mode) or (pending.st_dev, pending.st_ino) != source_identity:
            raise OSError(f"pending patch changed during no-clobber commit: {pending_absolute}")
        os.fsync(source_parent_fd)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if pending_parent_fd >= 0:
            os.close(pending_parent_fd)
        if source_parent_fd >= 0:
            os.close(source_parent_fd)


def _lock_timeout() -> float:
    raw = os.environ.get("OPENCOLLAB_HARNESS_LOCK_TIMEOUT_SECONDS", "10")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("invalid harness lock timeout") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid harness lock timeout")
    return value


def _acquire_output_lock(fd: int, path: Path) -> None:
    timeout = _lock_timeout()
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out acquiring prediction output lock after {timeout:g}s: {path}"
            )
        time.sleep(min(0.01, remaining))


def _validate_pending(
    record_path: Path,
    patch_path: Path,
    output: Path,
) -> tuple[dict, tuple[int, int], tuple[int, int] | None]:
    raw, record_identity = _read_regular_with_identity(
        record_path,
        MAX_PENDING_RECORD_BYTES,
        label="pending artifact",
    )
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pending prediction envelope is malformed") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != "opencollab.pending-prediction.v1"
        or not isinstance(envelope.get("record"), dict)
    ):
        raise ValueError("pending prediction envelope is malformed")
    record = envelope["record"]
    output_value = envelope.get("output_path")
    if not isinstance(output_value, str) or not output_value:
        raise ValueError("pending prediction has no output path")
    if output_value != os.path.abspath(output):
        raise ValueError("pending prediction output path does not match this run")
    patch = record.get("model_patch")
    if not isinstance(patch, str):
        raise ValueError("pending record has no patch text")
    patch_identity = None
    if os.path.lexists(patch_path):
        patch_raw, patch_identity = _read_regular_with_identity(
            patch_path,
            MAX_PENDING_PATCH_BYTES,
            label="pending artifact",
        )
        patch_copy = patch_raw.decode("utf-8", errors="strict")
        if patch_copy != patch:
            raise ValueError("pending patch and record payload disagree")
    digest = hashlib.sha256(
        patch.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    if record.get("patch_sha256") != digest or not record.get("record_id"):
        raise ValueError("pending prediction identity is invalid")
    metric = record.get("workflow_metric")
    if (
        not isinstance(metric, dict)
        or metric.get("instance_id") != record.get("instance_id")
        or metric.get("record_id") != record.get("record_id")
        or metric.get("patch_sha256") != digest
    ):
        raise ValueError("pending workflow metric identity is invalid")
    status = metric.get("workflow_status")
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ValueError("pending workflow metric has invalid runner return code")
    valid_status = (
        (status == "done" and returncode == 0 and bool(patch))
        or (status == "done_with_timeout_patch" and returncode == 124 and bool(patch))
        or (status == "empty_patch" and returncode in {0, 124} and not patch)
        or (status == "error" and returncode not in {0, 124})
    )
    if not valid_status:
        raise ValueError("pending workflow metric status/return code mismatch")
    for field in REQUIRED_TRUE_INTEGRITY_FIELDS:
        if metric.get(field) is not True:
            raise ValueError(
                f"pending workflow metric lacks true integrity proof: {field}"
            )
    for field in REQUIRED_FALSE_INTEGRITY_FIELDS:
        if metric.get(field) is not False:
            raise ValueError(
                f"pending workflow metric lacks false integrity proof: {field}"
            )
    if metric.get("worktree_integrity_proven") is not True:
        raise ValueError("pending workflow metric lacks worktree integrity proof")
    if metric.get("patch_produced") is not bool(patch.strip()):
        raise ValueError("pending workflow metric patch_produced disagrees with patch")
    return record, record_identity, patch_identity


def _parent_matches(path: Path, expected_fd: int) -> bool:
    try:
        _absolute, current_fd, _name = batch_io.open_parent(path, create=False)
    except OSError:
        return False
    try:
        current = os.fstat(current_fd)
        expected = os.fstat(expected_fd)
        return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
    finally:
        os.close(current_fd)


def flush_pending_prediction(record_path: Path, patch_path: Path, output: Path) -> int:
    record, record_identity, patch_identity = _validate_pending(
        record_path,
        patch_path,
        output,
    )
    metric = record["workflow_metric"]
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_JSONL_BYTES:
        raise ValueError("prediction output row exceeds byte limit")
    output_path, parent_fd, name = batch_io.open_parent(output, create=True)
    fd = -1
    locked = False
    try:
        fd, _created = batch_io.open_regular_at(
            parent_fd,
            name,
            os.O_RDWR | os.O_APPEND,
            0o644,
            label=output_path,
        )
        _acquire_output_lock(fd, output_path)
        locked = True
        if os.fstat(fd).st_size > MAX_OUTPUT_JSONL_BYTES:
            raise ValueError("prediction output exceeds byte limit")
        duplicate = False
        with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
            handle.seek(0)
            while True:
                line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_JSONL_LINE_BYTES:
                    raise ValueError("prediction output contains an oversized line")
                try:
                    existing = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "prediction output contains invalid UTF-8 JSONL"
                    ) from exc
                if not isinstance(existing, dict):
                    raise ValueError(
                        "prediction output contains a non-object JSONL row"
                    )
                if existing.get("record_id") == record["record_id"]:
                    if existing != record:
                        raise ValueError("record_id collision in prediction output")
                    duplicate = True
                    break
        if not duplicate:
            size = os.fstat(fd).st_size
            needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
            if size + int(needs_separator) + len(payload) > MAX_OUTPUT_JSONL_BYTES:
                raise ValueError("prediction output exceeds byte limit")
            if needs_separator:
                batch_io.write_all(fd, b"\n")
            batch_io.write_all(fd, payload)
        os.fsync(fd)
        current = batch_io.stat_at(parent_fd, name)
        opened = os.fstat(fd)
        if current is None or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise OSError("prediction output changed while appending")
        os.fsync(parent_fd)
        if not _parent_matches(output_path, parent_fd):
            raise OSError("prediction output parent changed while appending")
    finally:
        if fd >= 0:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        os.close(parent_fd)
    if patch_identity is not None:
        unlink_regular_file_durable(
            patch_path,
            expected_target_identity=patch_identity,
        )
    unlink_regular_file_durable(
        record_path,
        expected_target_identity=record_identity,
    )
    status = metric["workflow_status"]
    returncode = metric["runner_returncode"]
    completed = (status == "done" and returncode == 0) or (
        status == "done_with_timeout_patch" and returncode == 124
    )
    return 0 if completed else 1


def bounded_diff_command(
    swebench_dir: Path,
    *,
    workspace: Path | None = None,
    retirement_log: Path | None = None,
    helper_path: Path = Path("/tmp/opencollab_gen_prediction_bounded_capture.py"),
) -> str:
    sys.path.insert(0, str(swebench_dir))
    module = importlib.import_module("gen_prediction")
    command = "git diff --cached --binary"
    if any(
        value is not None
        for value in (workspace, retirement_log)
    ):
        if any(
            value is None
            for value in (workspace, retirement_log)
        ):
            raise ValueError("workspace and retirement log are required")
        assert workspace is not None
        assert retirement_log is not None
        command = team_staged_diff_command(workspace, retirement_log)
    return module.bounded_container_output_command(
        command,
        max_bytes=module.MAX_EXTRACTED_PATCH_BYTES,
        label="team staged patch",
        helper_path=str(helper_path),
    )


def team_staged_diff_command(
    workspace: Path,
    retirement_log: Path,
) -> str:
    """Build the team extractor after validating its bounded sidecar."""
    retirements = registered_retirement_paths(
        workspace,
        persistent_log=retirement_log,
    )
    return guarded_staged_diff_command(
        registered_retirement_paths=retirements,
    )


def create_retirement_log(path: Path) -> tuple[int, int]:
    create_regular_bytes_atomic(path, b"", max_bytes=0, mode=0o600)
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or current.st_size:
        raise OSError("internal retirement log is invalid")
    return current.st_dev, current.st_ino


def remove_retirement_log(path: Path, expected_identity: str) -> None:
    match = re.fullmatch(r"([0-9]+):([0-9]+)", expected_identity)
    if match is None:
        raise ValueError("internal retirement log identity is invalid")
    if not unlink_regular_file_durable(
        path,
        expected_target_identity=(int(match.group(1)), int(match.group(2))),
    ):
        raise OSError("internal retirement log disappeared before cleanup")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    timeout = commands.add_parser("validate-timeout")
    timeout.add_argument("value")
    directory = commands.add_parser("prepare-directory")
    directory.add_argument("candidate")
    directory.add_argument("containment", nargs="?", default="")
    task = commands.add_parser("prepare-task")
    task.add_argument("instance_file", type=Path)
    task.add_argument("task_file", type=Path)
    task.add_argument("--include-hints", choices=("0", "1"), required=True)
    image = commands.add_parser("validate-image")
    image.add_argument("value")
    key = commands.add_parser("session-key")
    key.add_argument("session_root")
    key.add_argument("instance_id")
    digest = commands.add_parser("instance-digest")
    digest.add_argument("instance_id")
    create = commands.add_parser("create-pending")
    create.add_argument("record", type=Path)
    create.add_argument("pending_patch", type=Path)
    create.add_argument("source_patch", type=Path)
    create.add_argument("output", type=Path)
    create.add_argument("instance_id")
    create.add_argument("model_name")
    create.add_argument("returncode", type=int)
    flush = commands.add_parser("flush-pending")
    flush.add_argument("record", type=Path)
    flush.add_argument("pending_patch", type=Path)
    flush.add_argument("output", type=Path)
    diff = commands.add_parser("bounded-diff-command")
    diff.add_argument("swebench_dir", type=Path)
    diff.add_argument("--workspace", type=Path)
    diff.add_argument("--retirement-log", type=Path)
    diff.add_argument("--helper-path", type=Path, default=Path("/tmp/opencollab_gen_prediction_bounded_capture.py"))
    create_retirement = commands.add_parser("create-retirement-log")
    create_retirement.add_argument("path", type=Path)
    remove_retirement = commands.add_parser("remove-retirement-log")
    remove_retirement.add_argument("path", type=Path)
    remove_retirement.add_argument("expected_identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-timeout":
            validate_timeout(args.value)
        elif args.command == "prepare-directory":
            print(prepare_real_directory(args.candidate, args.containment))
        elif args.command == "prepare-task":
            print(
                prepare_task(
                    args.instance_file,
                    args.task_file,
                    include_hints=args.include_hints == "1",
                )
            )
        elif args.command == "validate-image":
            print(validate_image(args.value))
        elif args.command == "session-key":
            print(session_key(args.session_root, args.instance_id))
        elif args.command == "instance-digest":
            print(instance_digest(args.instance_id))
        elif args.command == "create-pending":
            create_pending_prediction(
                args.record,
                args.pending_patch,
                args.source_patch,
                args.output,
                args.instance_id,
                args.model_name,
                args.returncode,
            )
        elif args.command == "flush-pending":
            print(flush_pending_prediction(args.record, args.pending_patch, args.output))
        elif args.command == "bounded-diff-command":
            print(
                bounded_diff_command(
                    args.swebench_dir,
                    workspace=args.workspace,
                    retirement_log=args.retirement_log,
                    helper_path=args.helper_path,
                )
            )
        elif args.command == "create-retirement-log":
            dev, ino = create_retirement_log(args.path)
            print(f"{dev}:{ino}")
        elif args.command == "remove-retirement-log":
            remove_retirement_log(args.path, args.expected_identity)
        else:
            raise AssertionError(args.command)
    except (OSError, ValueError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 125
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
