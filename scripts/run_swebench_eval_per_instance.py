#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import errno as errno
import fcntl as fcntl
import hashlib as hashlib
import json as json
import math as math
import os
import re as re
import select as select
import signal as signal
import stat as stat
import subprocess
import sys
import threading
import time
import unicodedata as unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from pathlib import PureWindowsPath as PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.adapters.safe_files import (  # noqa: E402
    _directory_path_matches_fd as _directory_path_matches_fd,
)
from opencollab.adapters.safe_files import (
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
)
from opencollab.adapters.safe_files import (
    read_regular_bytes as read_regular_bytes,
)
from opencollab.adapters.safe_files import (
    regular_path_identity as regular_path_identity,
)
from opencollab.adapters.safe_files import (
    unlink_regular_file_durable as unlink_regular_file_durable,
)
from opencollab.adapters.safe_files import (
    write_regular_bytes_atomic as write_regular_bytes_atomic,
)
from opencollab.harness import swe_eval_records as swe_records  # noqa: E402, F401
from opencollab.harness.swe_eval_records import (  # noqa: E402
    MAX_JSON_DOCUMENT_BYTES as MAX_JSON_DOCUMENT_BYTES,
)
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN as SUBMISSION_INTEGRITY_PROVEN,
)
from opencollab.harness.swe_eval_records import (
    RecordInputFormatError as RecordInputFormatError,
)
from opencollab.harness.swe_eval_records import (
    RecordInputLimitError as RecordInputLimitError,
)
from opencollab.harness.swe_eval_records import (
    UnsafeRecordInputError as UnsafeRecordInputError,
)
from opencollab.harness.swe_eval_records import (
    embedded_workflow_metric as embedded_workflow_metric,
)
from opencollab.harness.swe_eval_records import (
    is_completed_prediction as is_completed_prediction,
)
from opencollab.harness.swe_eval_records import (
    metric_submission_integrity as metric_submission_integrity,
)

_PROCESS_IDENTITY_POPEN = subprocess.Popen
_ORIGINAL_EVALUATOR_POPEN = subprocess.Popen
_EVALUATOR_POPEN = subprocess.Popen
PROCESS_TERM_GRACE_SECONDS = 30.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
PROCESS_CLEANUP_FAILED_EXIT_CODE = 125
PROCESS_SPAWN_TIMEOUT_SECONDS = 10.0
HELPER_RESIDUAL_TERM_GRACE_SECONDS = 0.1
PROCESS_IDENTITY_TIMEOUT_SECONDS = 5.0
MAX_PATH_IDENTITY_BYTES = 240
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_LINE_BYTES = 16 * 1024 * 1024
MAX_DATASET_ROWS = 10_000
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
TECHNICAL_REPORT_STATUSES = {
    "technical_eval_failed",
    "eval_failed",
    "eval_start_failed",
    "eval_driver_error",
    "empty_eval_patch_invalid",
    "blocked_missing_eval_deps",
    "blocked_missing_eval_image",
    "blocked_missing_eval_spec",
}



from scripts.swebench_eval_process import (  # noqa: E402
    ActiveProcessRegistry as ActiveProcessRegistry,
)
from scripts.swebench_eval_process import (
    EvaluatorSpawnTimeout as EvaluatorSpawnTimeout,
)
from scripts.swebench_eval_process import (
    OwnedEvaluatorProcess as OwnedEvaluatorProcess,
)
from scripts.swebench_eval_process import (
    _claim_residual_group_is_live as _claim_residual_group_is_live,
)
from scripts.swebench_eval_process import (
    _cleanup_raw_helper as _cleanup_raw_helper,
)
from scripts.swebench_eval_process import (
    _consume_process_exit as _consume_process_exit,
)
from scripts.swebench_eval_process import (
    _evaluator_helper_main as _evaluator_helper_main,
)
from scripts.swebench_eval_process import (
    _identity_helper_main as _identity_helper_main,
)
from scripts.swebench_eval_process import (
    _proc_process_start_identity as _proc_process_start_identity,
)
from scripts.swebench_eval_process import (
    _process_group_exists as _process_group_exists,
)
from scripts.swebench_eval_process import (
    _read_identity_helper_message as _read_identity_helper_message,
)
from scripts.swebench_eval_process import (
    _schedule_process_exit_consumer as _schedule_process_exit_consumer,
)
from scripts.swebench_eval_process import (
    _spawn_owned_evaluator as _spawn_owned_evaluator,
)
from scripts.swebench_eval_process import (
    _terminate_process_group_owned as _terminate_process_group_owned,
)
from scripts.swebench_eval_process import (
    _wait_for_owned_cleanup as _wait_for_owned_cleanup,
)
from scripts.swebench_eval_process import (
    _wait_for_process_group_exit as _wait_for_process_group_exit,
)
from scripts.swebench_eval_process import (
    _write_helper_status as _write_helper_status,
)
from scripts.swebench_eval_process import (
    ensure_process_group_quiesced_after_wait as ensure_process_group_quiesced_after_wait,
)
from scripts.swebench_eval_process import (
    process_start_identity as process_start_identity,
)
from scripts.swebench_eval_process import (
    terminate_process_group as terminate_process_group,
)
from scripts.swebench_eval_records import (  # noqa: E402
    _acquire_exclusive_lock as _acquire_exclusive_lock,
)
from scripts.swebench_eval_records import (
    _claim_path as _claim_path,
)
from scripts.swebench_eval_records import (
    _fsync_directory as _fsync_directory,
)
from scripts.swebench_eval_records import (
    _open_append_text as _open_append_text,
)
from scripts.swebench_eval_records import (
    _open_regular_file as _open_regular_file,
)
from scripts.swebench_eval_records import (
    _pid_is_active as _pid_is_active,
)
from scripts.swebench_eval_records import (
    _read_bounded_json_safe as _read_bounded_json_safe,
)
from scripts.swebench_eval_records import (
    _stat_fingerprint as _stat_fingerprint,
)
from scripts.swebench_eval_records import (
    _unlink_durable as _unlink_durable,
)
from scripts.swebench_eval_records import (
    _write_bytes_atomic as _write_bytes_atomic,
)
from scripts.swebench_eval_records import (
    _write_json_atomic as _write_json_atomic,
)
from scripts.swebench_eval_records import (
    acquire_claim as acquire_claim,
)
from scripts.swebench_eval_records import (
    candidate_predictions_path as candidate_predictions_path,
)
from scripts.swebench_eval_records import (
    file_fingerprint as file_fingerprint,
)
from scripts.swebench_eval_records import (
    identity_path as identity_path,
)
from scripts.swebench_eval_records import (
    load_eval_queue as load_eval_queue,
)
from scripts.swebench_eval_records import (
    nonnegative_int_arg as nonnegative_int_arg,
)
from scripts.swebench_eval_records import (
    positive_int_arg as positive_int_arg,
)
from scripts.swebench_eval_records import (
    positive_timeout_seconds as positive_timeout_seconds,
)
from scripts.swebench_eval_records import (
    prediction_identity as prediction_identity,
)
from scripts.swebench_eval_records import (
    prediction_is_eval_eligible as prediction_is_eval_eligible,
)
from scripts.swebench_eval_records import (
    read_dataset as read_dataset,
)
from scripts.swebench_eval_records import (
    read_jsonl as read_jsonl,
)
from scripts.swebench_eval_records import (
    release_claim as release_claim,
)
from scripts.swebench_eval_records import (
    report_is_done as report_is_done,
)
from scripts.swebench_eval_records import (
    report_path as report_path,
)
from scripts.swebench_eval_records import (
    update_claim_process as update_claim_process,
)
from scripts.swebench_eval_records import (
    validate_model_identity as validate_model_identity,
)
from scripts.swebench_eval_records import (
    validate_path_identity as validate_path_identity,
)
from scripts.swebench_eval_records import (
    write_candidate_prediction as write_candidate_prediction,
)
from scripts.swebench_eval_records import (
    write_identity as write_identity,
)


def run_one(
    *,
    iid: str,
    model_name: str,
    identity: dict,
    prediction: dict,
    ordinal: int,
    total: int,
    dataset_path: Path,
    work_dir: Path,
    run_id: str,
    timeout: int,
    namespace: str,
    cache_level: str,
    clean: str,
    outer_timeout: int,
    env: dict[str, str],
    print_lock: threading.Lock,
    active_processes: ActiveProcessRegistry | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[str, int]:
    iid = validate_path_identity(iid, name="instance_id")
    model_name = validate_model_identity(model_name)
    run_id = validate_path_identity(run_id, name="run_id")
    identity_iid = validate_path_identity(
        identity.get("instance_id") or "",
        name="identity.instance_id",
    )
    prediction_iid = validate_path_identity(
        prediction.get("instance_id") or "",
        name="prediction.instance_id",
    )
    prediction_model = validate_model_identity(
        prediction.get("model_name_or_path") or "unknown-model"
    )
    if identity_iid != iid or prediction_iid != iid:
        raise ValueError("instance_id does not match prediction identity")
    if prediction_model != model_name:
        raise ValueError("model_name_or_path does not match prediction")
    official_report = report_path(work_dir, run_id, model_name, iid)
    if report_is_done(official_report, iid, identity):
        with print_lock:
            print(f"[{ordinal}/{total}] skipping {iid} (report exists)", flush=True)
        return iid, 0

    log_path = work_dir / "command_logs" / f"{iid}.log"
    ensure_directory_no_symlinks(log_path.parent)
    report_dir = work_dir / "reports" / iid
    candidate_path = write_candidate_prediction(work_dir, prediction, identity)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_swebench_eval_with_docker_timeout.py"),
        "-d",
        str(dataset_path),
        "-s",
        "test",
        "-i",
        iid,
        "-p",
        str(candidate_path),
        "--max_workers",
        "1",
        "-t",
        str(timeout),
        "--cache_level",
        cache_level,
        "--clean",
        clean,
        "-id",
        run_id,
        "-n",
        namespace,
        "--report_dir",
        str(report_dir),
    ]
    owner_token = uuid.uuid4().hex
    acquired, claim_path = acquire_claim(
        work_dir,
        iid,
        identity,
        lease_seconds=outer_timeout + 60,
        owner_token=owner_token,
    )
    if not acquired:
        with print_lock:
            print(f"[{ordinal}/{total}] skipping {iid} (evaluation already claimed)", flush=True)
        return iid, 0
    release_owned_claim = True

    def retain_residual_claim(pgid: int, start_identity: str) -> None:
        nonlocal release_owned_claim
        release_owned_claim = False
        update_claim_process(
            claim_path,
            owner_token=owner_token,
            evaluator_pgid=pgid,
            evaluator_start_identity=start_identity,
            status="cleanup_failed",
            lease_seconds=max(300, outer_timeout + 60),
        )

    with print_lock:
        print(f"[{ordinal}/{total}] evaluating {iid}", flush=True)
    started_at_ns = time.time_ns()
    prior_report_fingerprint = file_fingerprint(official_report)
    attempt_path = identity_path(official_report)
    try:
        if stop_event is not None and stop_event.is_set():
            return iid, 130
        write_identity(
            attempt_path,
            identity,
            status="launching",
            pid=os.getpid(),
            started_at_ns=started_at_ns,
            prior_report_fingerprint=prior_report_fingerprint,
        )
        with _open_append_text(log_path) as log_file:
            log_file.write("\n\n$ " + " ".join(cmd) + "\n")
            log_file.write(f"# outer_timeout={outer_timeout}s\n")
            try:
                if (
                    os.name == "posix"
                    and subprocess.Popen is _ORIGINAL_EVALUATOR_POPEN
                ):
                    process = _spawn_owned_evaluator(
                        cmd,
                        cwd=work_dir,
                        env=env,
                        log_fd=log_file.fileno(),
                        wall_timeout=outer_timeout,
                    )
                else:
                    process = subprocess.Popen(
                        cmd,
                        cwd=work_dir,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except EvaluatorSpawnTimeout as exc:
                log_file.write(f"evaluator spawn timed out: {exc}\n")
                write_identity(
                    attempt_path,
                    identity,
                    status="failed_to_start",
                    started_at_ns=started_at_ns,
                    prior_report_fingerprint=prior_report_fingerprint,
                )
                return iid, PROCESS_CLEANUP_FAILED_EXIT_CODE
            except OSError as exc:
                log_file.write(f"failed to start evaluator: {exc}\n")
                write_identity(
                    attempt_path,
                    identity,
                    status="failed_to_start",
                    started_at_ns=started_at_ns,
                    prior_report_fingerprint=prior_report_fingerprint,
                )
                return iid, 127
            evaluator_start_identity = ""
            try:
                if active_processes is not None:
                    active_processes.add(process)
                evaluator_start_identity = process_start_identity(process.pid)
                claim_updated = update_claim_process(
                    claim_path,
                    owner_token=owner_token,
                    evaluator_pgid=process.pid,
                    evaluator_start_identity=evaluator_start_identity,
                    status="running",
                    lease_seconds=outer_timeout + 60,
                )
                if not claim_updated:
                    log_file.write(
                        "failed to persist evaluator process identity in claim\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    return iid, (
                        4
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                if stop_event is not None and stop_event.is_set():
                    log_file.write(
                        "evaluation stop requested after evaluator start; "
                        "terminating process group\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                        try:
                            write_identity(
                                attempt_path,
                                identity,
                                status="cleanup_failed",
                                pid=process.pid,
                                started_at_ns=started_at_ns,
                                prior_report_fingerprint=prior_report_fingerprint,
                            )
                        except Exception:
                            pass
                    return iid, (
                        130
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                try:
                    write_identity(
                        attempt_path,
                        identity,
                        status="started",
                        pid=process.pid,
                        started_at_ns=started_at_ns,
                        prior_report_fingerprint=prior_report_fingerprint,
                    )
                except Exception as exc:
                    log_file.write(f"failed to persist started evaluator identity: {exc}\n")
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    try:
                        write_identity(
                            attempt_path,
                            identity,
                            status="identity_persist_failed",
                            started_at_ns=started_at_ns,
                            prior_report_fingerprint=prior_report_fingerprint,
                        )
                    except Exception:
                        pass
                    return iid, (
                        4 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                try:
                    returncode = process.wait(timeout=outer_timeout)
                    cleanup_quiesced = ensure_process_group_quiesced_after_wait(
                        process,
                        log_file,
                    )
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                        returncode = PROCESS_CLEANUP_FAILED_EXIT_CODE
                except subprocess.TimeoutExpired:
                    log_file.write(
                        f"\nouter timeout after {outer_timeout}s; terminating process group\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    returncode = (
                        124
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                except Exception as exc:
                    log_file.write(
                        f"evaluator wait failed: {type(exc).__name__}: {exc}\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    returncode = (
                        4 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                if (
                    stop_event is not None
                    and stop_event.is_set()
                    and _process_group_exists(process.pid)
                ):
                    log_file.write(
                        "stop requested and evaluator descendants remain active; "
                        "retaining claim\n"
                    )
                    retain_residual_claim(
                        process.pid,
                        evaluator_start_identity,
                    )
                    returncode = PROCESS_CLEANUP_FAILED_EXIT_CODE
            except BaseException:
                cleanup_quiesced = False
                try:
                    cleanup_quiesced = terminate_process_group(process, log_file)
                except BaseException:
                    pass
                if not cleanup_quiesced:
                    retain_residual_claim(
                        process.pid,
                        evaluator_start_identity,
                    )
                    try:
                        write_identity(
                            attempt_path,
                            identity,
                            status="cleanup_failed",
                            pid=process.pid,
                            started_at_ns=started_at_ns,
                            prior_report_fingerprint=prior_report_fingerprint,
                        )
                    except Exception:
                        pass
                raise
            finally:
                if active_processes is not None:
                    active_processes.discard(process)
        if returncode == 0 and not report_is_done(official_report, iid, identity):
            returncode = 3
            with _open_append_text(log_path) as log_file:
                log_file.write("evaluator exited 0 without an exact-candidate report\n")
        if returncode == PROCESS_CLEANUP_FAILED_EXIT_CODE:
            final_status = "cleanup_failed"
            final_pid = process.pid
        else:
            final_status = "completed" if returncode == 0 else "failed"
            final_pid = 0
        try:
            write_identity(
                attempt_path,
                identity,
                status=final_status,
                pid=final_pid,
                started_at_ns=started_at_ns,
                prior_report_fingerprint=prior_report_fingerprint,
            )
        except Exception as exc:
            _unlink_durable(attempt_path)
            with _open_append_text(log_path) as log_file:
                log_file.write(
                    f"failed to persist evaluator final identity: "
                    f"{type(exc).__name__}: {exc}\n"
                )
            returncode = 4
    finally:
        if release_owned_claim:
            release_claim(claim_path, owner_token=owner_token)
    if returncode == 0:
        with print_lock:
            print(f"done {iid}", flush=True)
    else:
        with print_lock:
            print(f"failed {iid} exit={returncode}; see {log_path}", flush=True)
    return iid, returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SWE-bench official evaluation one instance at a time")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=positive_int_arg, default=1800)
    parser.add_argument("--limit", type=nonnegative_int_arg, default=0)
    parser.add_argument("--namespace", default="swebench")
    parser.add_argument("--cache-level", default="instance")
    parser.add_argument("--clean", default="False")
    parser.add_argument("--workers", type=positive_int_arg, default=1)
    parser.add_argument(
        "--outer-timeout",
        type=nonnegative_int_arg,
        default=0,
        help="Wall-clock timeout per subprocess in seconds. Defaults to --timeout + 900.",
    )
    args = parser.parse_args()

    try:
        args.run_id = validate_path_identity(args.run_id, name="run_id")
    except ValueError as exc:
        parser.error(str(exc))

    dataset_path = Path(os.path.abspath(args.dataset))
    predictions_path = Path(os.path.abspath(args.predictions))
    work_dir = Path(os.path.abspath(args.work_dir))
    try:
        for input_path in (dataset_path, predictions_path):
            parent_fd = _open_directory_no_symlinks(input_path.parent)
            os.close(parent_fd)
        ensure_directory_no_symlinks(work_dir)
        ensure_directory_no_symlinks(work_dir / "command_logs")
    except OSError as exc:
        parser.error(f"unsafe input or work directory: {exc}")

    try:
        queue = load_eval_queue(
            dataset_path,
            predictions_path,
            args.run_id,
            work_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit > 0:
        queue = queue[: args.limit]
    print(f"pending_non_empty_instances={len(queue)}")

    env = os.environ.copy()
    env.setdefault("OPENCOLLAB_DOCKER_API_TIMEOUT", "900")
    env.setdefault("DOCKER_CLIENT_TIMEOUT", "900")
    try:
        for key in ("OPENCOLLAB_DOCKER_API_TIMEOUT", "DOCKER_CLIENT_TIMEOUT"):
            env[key] = str(positive_timeout_seconds(env[key], name=key))
    except ValueError as exc:
        parser.error(str(exc))
    env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    workers = args.workers
    outer_timeout = args.outer_timeout if args.outer_timeout > 0 else args.timeout + 900
    print_lock = threading.Lock()
    failures: list[tuple[str, int]] = []
    stop_event = threading.Event()
    active_processes = ActiveProcessRegistry()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    try:
        futures = {
            pool.submit(
                run_one,
                iid=iid,
                model_name=model_name,
                identity=identity,
                prediction=prediction,
                ordinal=index,
                total=len(queue),
                dataset_path=dataset_path,
                work_dir=work_dir,
                run_id=args.run_id,
                timeout=args.timeout,
                namespace=args.namespace,
                cache_level=args.cache_level,
                clean=args.clean,
                outer_timeout=outer_timeout,
                env=env,
                print_lock=print_lock,
                active_processes=active_processes,
                stop_event=stop_event,
            ): iid
            for index, (iid, model_name, identity, prediction) in enumerate(queue, 1)
        }
        for future in as_completed(futures):
            expected_iid = futures[future]
            try:
                iid, returncode = future.result()
            except Exception as exc:
                iid, returncode = expected_iid, 70
                print(
                    f"failed {iid} with unhandled {type(exc).__name__}: {exc}",
                    flush=True,
                )
            if returncode != 0:
                failures.append((iid, returncode))
    except BaseException:
        stop_event.set()
        for future in futures:
            future.cancel()
        try:
            active_processes.terminate_all(sys.stderr)
        except BaseException:
            pass
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    if failures:
        print("failures=" + ", ".join(f"{iid}:{code}" for iid, code in failures), flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
