"""One-task generation execution for the V1 remote runner."""

# ruff: noqa: F403, F405

from opencollab.harness import swe_v1_remote_cleanup as remote_cleanup
from opencollab.harness.swe_v1_remote_commands import *
from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_records import *
from opencollab.harness.swe_v1_remote_state import *


def bind_eval_container_marker(cidfile, marker_path, container_name, proc, timeout=2.0):
    deadline = time.monotonic() + timeout
    last_error = "container cidfile did not appear"
    while time.monotonic() < deadline:
        try:
            raw = remote_cleanup.read_bounded_regular(
                cidfile,
                max_bytes=remote_cleanup.MAX_CONTAINER_REFERENCE_BYTES,
            )
            container_id = raw.decode("ascii").strip().lower()
        except FileNotFoundError:
            container_id = ""
        except (OSError, UnicodeDecodeError, remote_cleanup.CleanupInputError) as exc:
            return {"ok": False, "status": "invalid_cidfile", "details": str(exc)}
        if remote_cleanup.FULL_CONTAINER_ID_RE.fullmatch(container_id):
            try:
                marker = remote_cleanup.read_bounded_json(
                    marker_path,
                    max_bytes=remote_cleanup.MAX_CONTAINER_MARKER_BYTES,
                )
            except (OSError, remote_cleanup.CleanupInputError) as exc:
                return {"ok": False, "status": "invalid_marker", "details": str(exc)}
            if (
                marker.get("schema") != remote_cleanup.EVAL_CONTAINER_SCHEMA
                or marker.get("state") != "pending"
                or marker.get("container_name") != container_name
                or marker.get("owner_nonce") != owner_nonce
                or marker.get("owner_label") != remote_cleanup.EVAL_OWNER_LABEL
                or marker.get("owner_schema_label") != remote_cleanup.EVAL_SCHEMA_LABEL
                or marker.get("owner_schema") != remote_cleanup.EVAL_SCHEMA_LABEL_VALUE
            ):
                return {"ok": False, "status": "invalid_marker_ownership"}
            write_json(
                marker_path,
                {
                    **marker,
                    "state": "active",
                    "container_id": container_id,
                    "bound_at": now(),
                },
            )
            return {"ok": True, "container_id": container_id}
        if container_id:
            last_error = "container cidfile did not contain a complete id"
        poll = getattr(proc, "poll", None)
        if not callable(poll) or poll() is not None:
            break
        time.sleep(0.01)
    return {"ok": False, "status": "container_identity_unavailable", "details": last_error}


def clear_pending_eval_marker(cidfile, marker_path, container_name):
    try:
        cidfile.lstat()
    except FileNotFoundError:
        pass
    else:
        return {"ok": False, "status": "cidfile_exists"}
    try:
        marker = remote_cleanup.read_bounded_json(
            marker_path,
            max_bytes=remote_cleanup.MAX_CONTAINER_MARKER_BYTES,
        )
    except (FileNotFoundError, OSError, remote_cleanup.CleanupInputError) as exc:
        return {"ok": False, "status": "invalid_pending_marker", "details": str(exc)}
    if (
        marker.get("schema") != remote_cleanup.EVAL_CONTAINER_SCHEMA
        or marker.get("state") != "pending"
        or marker.get("container_name") != container_name
        or marker.get("container_id") != ""
        or marker.get("owner_nonce") != owner_nonce
        or marker.get("owner_label") != remote_cleanup.EVAL_OWNER_LABEL
        or marker.get("owner_schema_label") != remote_cleanup.EVAL_SCHEMA_LABEL
        or marker.get("owner_schema") != remote_cleanup.EVAL_SCHEMA_LABEL_VALUE
    ):
        return {"ok": False, "status": "pending_marker_ownership_unproven"}
    try:
        marker_path.unlink()
    except OSError as exc:
        return {"ok": False, "status": "pending_marker_unlink_failed", "details": str(exc)}
    return {"ok": True, "status": "pending_marker_removed"}


def generation_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True, exist_ok=True)
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(task, prediction, metric, pairing)
    if start_count(run_dir) >= max_task_starts:
        return {"status": "generation_start_limit_reached", "task": task, "start_count": start_count(run_dir)}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_generation_image", "task": task, "image_status": image_status}
    if dry_run:
        workdir_status = image_repo_workdir_status(image)
        if not workdir_status.get("ok"):
            return {
                "status": "blocked_bad_generation_workdir",
                "task": task,
                "image_status": image_status,
                "workdir_status": workdir_status,
            }
        return {"status": "would_generate", "task": task, "image": image, "workdir_status": workdir_status}
    fifo = pathlib.Path("/tmp") / (f"opencollab_v1_{os.getpid()}_{uuid.uuid4().hex}.fifo")
    os.mkfifo(fifo, 0o600)
    ACTIVE_FIFO_PATHS.add(fifo)
    session = task_session(task)
    state = write_start_state(run_dir, task, session)
    log_path = run_dir / "generation_logs" / f"{task}.outer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "OPENCOLLAB_SWE_GENERATOR": "workflow",
            "OPENCOLLAB_SWE_WORKFLOW": workflow,
            "OPENCOLLAB_SWE_MODEL_NAME": model_name,
            "OPENCOLLAB_SWE_BUDGET": str(budget),
            "OPENCOLLAB_SWE_MAX_STEPS": str(max_steps),
            "OPENCOLLAB_SWE_TIMEOUT": str(swe_timeout),
            "OPENCOLLAB_LLM_TIMEOUT": str(cfg["llm_timeout"]),
            "OPENCOLLAB_SWE_DATASET": "swe-batch-pro-lite",
            "OPENCOLLAB_REMOTE_PROXY_BASE_URL": remote_proxy_base_url,
            "OPENCOLLAB_SWE_CHECKPOINT_INTERVAL_SECONDS": str(checkpoint_interval),
            "OPENCOLLAB_REMOTE_ROOT": str(remote_root),
            "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
        }
    )
    cmd = [
        str(remote_repo / "scripts" / "run_swe_v2_one_from_fifo.sh"),
        task,
        image,
        str(fifo),
        str(run_dir),
    ]
    with open_locked_append(log_path) as log:
        log.write(("\n===== generation start " + now() + " =====\n").encode())
        spawn_signal_state = block_spawn_signals()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(remote_root), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        except OSError as exc:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_fifo(fifo)
            return {
                "status": "generation_start_failed",
                "task": task,
                "details": str(exc),
                "log": str(log_path),
                "start_state": state,
            }
        except BaseException:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_fifo(fifo)
            raise
        ACTIVE_CHILD_PGIDS.add(proc.pid)
        cleanup_quiesced = True
        try:
            restore_spawn_signals(spawn_signal_state)
            fifo_write = write_fifo_with_timeout(fifo, token + "\n")
            if not fifo_write.get("ok"):
                cleanup_quiesced = terminate_process_group_bounded(proc)
                cleanup_fifo(fifo)
                if not cleanup_quiesced:
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "details": fifo_write,
                        "log": str(log_path),
                    }
                return {"status": "fifo_write_failed", "task": task, "details": fifo_write, "log": str(log_path)}
            try:
                returncode = proc.wait(timeout=task_wall_timeout)
                cleanup_quiesced = ensure_process_group_quiesced_after_wait(proc)
                if not cleanup_quiesced:
                    cleanup_fifo(fifo)
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "details": "generator leader exited with residual process-group descendants",
                        "log": str(log_path),
                        "start_state": state,
                    }
            except subprocess.TimeoutExpired:
                log.write(("\nouter generation timeout after " + str(task_wall_timeout) + "s\n").encode())
                cleanup_quiesced = terminate_process_group_bounded(proc)
                if not cleanup_quiesced:
                    cleanup_fifo(fifo)
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "log": str(log_path),
                        "start_state": state,
                    }
                done, prediction, metric, pairing = generation_done(run_dir, task)
                cleanup_fifo(fifo)
                if done:
                    return generation_done_result(
                        task,
                        prediction,
                        metric,
                        pairing,
                        returncode=124,
                        log=str(log_path),
                        start_state=state,
                        timed_out=True,
                    )
                return {
                    "status": "generation_timeout",
                    "task": task,
                    "returncode": 124,
                    "log": str(log_path),
                    "start_state": state,
                }
        except BaseException:
            cleanup_quiesced = False
            try:
                cleanup_quiesced = terminate_process_group_bounded(proc)
            except BaseException:
                pass
            cleanup_fifo(fifo)
            raise
        finally:
            if cleanup_quiesced:
                ACTIVE_CHILD_PGIDS.discard(proc.pid)
    cleanup_fifo(fifo)
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(
            task,
            prediction,
            metric,
            pairing,
            returncode=returncode,
            log=str(log_path),
            start_state=state,
        )
    return {
        "status": "generation_failed",
        "task": task,
        "returncode": returncode,
        "log": str(log_path),
        "pairing": pairing,
        "patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
        "start_state": state,
    }


def _summary_plan_evidence_valid(tests_status, prefix, expected_plan):
    if not isinstance(expected_plan, dict):
        return False
    reported_plan = tests_status.get(f"{prefix}_plan")
    evidence = tests_status.get(f"{prefix}_evidence")
    if reported_plan != expected_plan or not isinstance(evidence, list):
        return False
    commands = expected_plan.get("commands")
    if not isinstance(commands, list) or len(evidence) != len(commands):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            return False
        status = item.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            return False
        if not all(
            item.get(field) is True
            for field in (
                "command_matches_plan",
                "log_artifact_safe",
                "target_proof_matches_plan",
                "artifact_safe",
            )
        ):
            return False
    return True


def eval_summary_matches_prediction(
    summary,
    prediction,
    task,
    *,
    eval_spec_sha256="",
    f2p_plan=None,
    p2p_plan=None,
):
    if (
        not isinstance(summary, dict)
        or summary.get("schema") != "opencollab.prolite_direct_eval.v2"
        or summary.get("status") != "done"
        or not isinstance(summary.get("resolved"), bool)
        or summary.get("technical_reasons") != []
        or summary.get("output_artifact_errors") != []
        or summary.get("docker_exit") != 0
        or summary.get("cleanup_quiesced") is not True
        or not isinstance(summary.get("container_cleanup"), dict)
        or summary["container_cleanup"].get("ok") is not True
    ):
        return False
    if not eval_model_patch(prediction).strip():
        return False
    if summary.get("task") and summary.get("task") != task:
        return False
    current_sha = row_patch_sha(prediction)
    previous_sha = str(summary.get("patch_sha256") or "")
    if not patch_sha_matches(previous_sha, current_sha):
        return False
    current_record = row_record_id(prediction)
    previous_record = str(summary.get("record_id") or "")
    if current_record and previous_record and current_record != previous_record:
        return False
    if current_record and not previous_record:
        return False
    if eval_spec_sha256 and summary.get("eval_spec_sha256") != eval_spec_sha256:
        return False
    tests_status = summary.get("tests_status")
    if not isinstance(tests_status, dict):
        return False
    if any(
        tests_status.get(field) != 0
        for field in (
            "service_bootstrap_status",
            "before_repo_status",
            "model_patch_status",
            "test_patch_status",
        )
    ):
        return False
    if not _summary_plan_evidence_valid(tests_status, "fail_to_pass", f2p_plan):
        return False
    if not _summary_plan_evidence_valid(tests_status, "pass_to_pass", p2p_plan):
        return False
    f2p_evidence = tests_status["fail_to_pass_evidence"]
    p2p_evidence = tests_status["pass_to_pass_evidence"]
    f2p_status = tests_status.get("fail_to_pass_status")
    p2p_status = tests_status.get("pass_to_pass_status")
    if (
        isinstance(f2p_status, bool)
        or not isinstance(f2p_status, int)
        or isinstance(p2p_status, bool)
        or not isinstance(p2p_status, int)
    ):
        return False
    if f2p_status != next((item["status"] for item in f2p_evidence if item["status"] != 0), 0):
        return False
    if p2p_status != next((item["status"] for item in p2p_evidence if item["status"] != 0), 0):
        return False
    expected_resolved = bool(
        f2p_status == 0
        and p2p_status == 0
        and all(item["status"] == 0 for item in f2p_evidence)
        and all(item["status"] == 0 for item in p2p_evidence)
    )
    return summary["resolved"] is expected_resolved


def eval_log_has_infra_failure(exit_status, log_text):
    if exit_status in {124, 126, 127}:
        return True
    if exit_status == 0:
        return False
    patterns = (
        r"\bconnectionrefusederror\b",
        r"\bconnection refused\b",
        r"\btemporary failure in name resolution\b",
        r"\bname or service not known\b",
        r"\bnetwork is unreachable\b",
        r"\bno space left on device\b",
        r"\bcannot connect to the docker daemon\b",
        r"\b(?:redis|mongodb?|postgres(?:ql)?|mysql|database)\b.{0,100}"
        r"\b(?:unavailable|refused|failed to connect|not running|timed out)\b",
    )
    for raw_line in str(log_text or "").splitlines():
        line = raw_line.lower()
        if "assertionerror" in line:
            continue
        if any(re.search(pattern, line) for pattern in patterns):
            return True
    return False


def _eval_container_state(candidate):
    owner_label = candidate["owner_label"]
    owner_schema_label = candidate["owner_schema_label"]
    inspect_format = (
        '{{.Id}}\\t{{index .Config.Labels "'
        + owner_label
        + '"}}\\t{{index .Config.Labels "'
        + owner_schema_label
        + '"}}'
    )
    result = run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            inspect_format,
            "--",
            candidate["container_id"],
        ],
        timeout=30,
    )
    details = str(result.get("stderr") or result.get("stdout") or "")
    if result.get("returncode") != 0:
        lowered = details.lower()
        if "no such container" in lowered or "no such object" in lowered:
            return {"ok": True, "absent": True}
        return {
            "ok": False,
            "absent": False,
            "status": "inspect_failed",
            "details": details[-1000:],
        }
    parts = str(result.get("stdout") or "").split("\\t")
    if (
        len(parts) != 3
        or parts[0].lower() != candidate["container_id"]
        or parts[1] != candidate["owner_nonce"]
        or parts[2] != candidate["owner_schema"]
    ):
        return {
            "ok": False,
            "absent": False,
            "status": "ownership_unproven",
        }
    return {"ok": True, "absent": False}


def cleanup_eval_container(cidfile, marker_path, container_name):
    try:
        candidate = remote_cleanup.read_eval_container_marker(
            marker_path,
            expected_runner_nonce=owner_nonce,
        )
    except (FileNotFoundError, OSError, remote_cleanup.CleanupInputError) as exc:
        return {
            "ok": False,
            "status": "ownership_unproven",
            "details": str(exc),
            "marker_path": str(marker_path),
            "cidfile": str(cidfile),
        }
    if container_name and candidate["container_name"] != container_name:
        return {
            "ok": False,
            "status": "ownership_unproven",
            "details": "container name does not match its ownership marker",
            "marker_path": str(marker_path),
            "cidfile": str(cidfile),
        }

    before = _eval_container_state(candidate)
    attempts = [{"phase": "before", **before}]
    if not before.get("ok"):
        return {
            "ok": False,
            "status": str(before.get("status") or "inspect_failed"),
            "attempts": attempts,
            "marker_path": str(marker_path),
            "cidfile": str(cidfile),
        }
    if not before.get("absent"):
        removal = run(
            ["docker", "rm", "-f", "--", candidate["container_id"]],
            timeout=60,
        )
        attempts.append(
            {
                "phase": "remove",
                "returncode": removal.get("returncode"),
                "details": str(removal.get("stderr") or removal.get("stdout") or "")[-1000:],
            }
        )
    after = _eval_container_state(candidate)
    attempts.append({"phase": "after", **after})
    if not after.get("ok") or not after.get("absent"):
        return {
            "ok": False,
            "status": "remove_failed",
            "attempts": attempts,
            "marker_path": str(marker_path),
            "cidfile": str(cidfile),
        }
    try:
        cidfile.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "status": "marker_cleanup_failed",
            "details": str(exc),
            "attempts": attempts,
        }
    return {
        "ok": True,
        "status": "all_references_absent",
        "attempts": attempts,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
