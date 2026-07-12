"""Official evaluation execution, summary, and remote CLI logic."""

# ruff: noqa: E501, F403, F405

from opencollab.harness import swe_v1_remote_cleanup as remote_cleanup
from opencollab.harness.swe_v1_remote_artifacts import (
    derive_eval_verdict,
    read_eval_output_artifacts,
)
from opencollab.harness.swe_v1_remote_commands import *
from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_eval_script import direct_eval_script
from opencollab.harness.swe_v1_remote_generation import *
from opencollab.harness.swe_v1_remote_records import *
from opencollab.harness.swe_v1_remote_state import *


def eval_for_task_once(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    eval_dir = run_dir / eval_dir_name
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    done, prediction, metric, pairing = generation_done(
        run_dir,
        task,
        require_identity=not eval_only,
    )
    if not done:
        if prediction is not None and metric is not None:
            original_model_patch = prediction_patch(prediction)
            model_patch = eval_model_patch(prediction)
            status = workflow_status(metric)
            if (
                original_model_patch.strip()
                and not model_patch.strip()
                and status in {"done", "done_with_timeout_patch"}
            ):
                summary = {
                    "schema": "opencollab.prolite_direct_eval.v2",
                    "status": "empty_eval_patch_invalid",
                    "task": task,
                    "resolved": False,
                    "patch_sha256": row_patch_sha(prediction),
                    "record_id": row_record_id(prediction),
                    "model_patch_chars": len(original_model_patch),
                    "eval_model_patch_chars": 0,
                    "technical_reasons": ["empty_eval_patch_after_filter"],
                    "pairing": pairing,
                }
                write_json(summary_path, summary)
                return {"status": "empty_eval_patch_invalid", "task": task, "summary": summary}
        return {"status": "skipped_no_generation_patch", "task": task, "pairing": pairing}
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    if not fail_to_pass:
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "blocked_missing_eval_spec",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "technical_reasons": ["missing_fail_to_pass"],
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {"status": "blocked_missing_eval_spec", "task": task, "summary": summary}
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    f2p_plan = prolite_test_plan(
        row,
        fail_to_pass,
        target_file="/eval_input/f2p.targets.json",
    )
    p2p_plan = prolite_test_plan(
        row,
        pass_to_pass,
        target_file="/eval_input/p2p.targets.json",
    )
    eval_spec_sha256 = prolite_eval_spec_sha256(row, f2p_plan, p2p_plan)
    unverified_plan_reasons = []
    if not f2p_plan["coverage_verified"]:
        unverified_plan_reasons.append("no_verified_fail_to_pass_plan")
    if pass_to_pass and not p2p_plan["coverage_verified"]:
        unverified_plan_reasons.append("no_verified_pass_to_pass_plan")
    if unverified_plan_reasons:
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "technical_eval_failed",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "eval_spec_sha256": eval_spec_sha256,
            "technical_reasons": unverified_plan_reasons,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {"status": "technical_eval_failed", "task": task, "summary": summary}
    previous = load_json(summary_path)
    if (
        isinstance(previous, dict)
        and previous.get("eval_spec_sha256") == eval_spec_sha256
        and eval_summary_matches_prediction(
            previous,
            prediction,
            task,
            eval_spec_sha256=eval_spec_sha256,
            f2p_plan=f2p_plan,
            p2p_plan=p2p_plan,
        )
    ):
        return {"status": "eval_done", "task": task, "summary": previous, "report_path": str(report_path)}
    if dry_run:
        return {"status": "would_eval", "task": task, "executed": False}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {
            "status": "blocked_missing_eval_image",
            "task": task,
            "image_status": image_status,
            "executed": False,
        }
    input_dir = eval_dir / "input"
    output_dir = report_path.parent
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    proof_nonce = uuid.uuid4().hex
    original_model_patch = prediction_patch(prediction)
    model_patch = eval_model_patch(prediction)
    test_patch = str(row.get("test_patch") or "")
    f2p_cmd = " && ".join(f2p_plan["commands"])
    p2p_cmd = " && ".join(p2p_plan["commands"])
    service_bootstrap = prolite_service_bootstrap(row)
    atomic_write_bytes(input_dir / "model.patch", model_patch.encode("utf-8"))
    atomic_write_bytes(input_dir / "test.patch", test_patch.encode("utf-8"))
    atomic_write_bytes(
        input_dir / "service_bootstrap.sh",
        service_bootstrap.encode("utf-8"),
    )
    atomic_write_bytes(
        input_dir / "before_repo.sh",
        str(row.get("before_repo_set_cmd") or "").encode("utf-8"),
    )
    atomic_write_bytes(
        input_dir / "base_commit",
        (str(row.get("base_commit") or row.get("commit") or "").strip() + "\n").encode("utf-8"),
    )
    atomic_write_bytes(input_dir / "f2p.command", (f2p_cmd + "\n").encode("utf-8"))
    atomic_write_bytes(input_dir / "p2p.command", (p2p_cmd + "\n").encode("utf-8"))
    write_json(input_dir / "f2p.targets.json", fail_to_pass)
    write_json(input_dir / "p2p.targets.json", pass_to_pass)
    atomic_write_bytes(
        input_dir / "opencollab_pytest_proof.py",
        prolite_pytest_proof_plugin_source().encode("utf-8"),
    )
    atomic_write_bytes(input_dir / "proof.nonce", (proof_nonce + "\n").encode("ascii"))
    atomic_write_bytes(
        input_dir / "f2p.sh",
        prolite_test_plan_script(f2p_plan, "f2p", proof_nonce).encode("utf-8"),
    )
    atomic_write_bytes(
        input_dir / "p2p.sh",
        prolite_test_plan_script(p2p_plan, "p2p", proof_nonce).encode("utf-8"),
    )
    write_json(input_dir / "f2p.plan.json", f2p_plan)
    write_json(input_dir / "p2p.plan.json", p2p_plan)
    inner = direct_eval_script()
    script_path = input_dir / "run_prolite_direct_eval.sh"
    atomic_write_bytes(script_path, inner.encode("utf-8"))
    script_path.chmod(0o755)
    command_log = eval_dir / "command.log"
    cidfile = eval_dir / "container.cid"
    marker_path = eval_dir / "container.marker.json"
    previous_marker = load_json(marker_path)
    if isinstance(previous_marker, dict):
        previous_name = str(previous_marker.get("container_name") or "")
        stale_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            previous_name,
        )
        if not stale_cleanup.get("ok"):
            summary = {
                "schema": "opencollab.prolite_direct_eval.v2",
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    elif marker_path.exists() or cidfile.exists():
        stale_cleanup = cleanup_eval_container(cidfile, marker_path, "")
        if not stale_cleanup.get("ok"):
            summary = {
                "schema": "opencollab.prolite_direct_eval.v2",
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    container_name = (
        "opencollab-prolite-"
        + hashlib.sha256(f"{base_run_dir}:{task}:{os.getpid()}:{time.time_ns()}".encode()).hexdigest()[:24]
    )
    cidfile.unlink(missing_ok=True)
    write_json(
        marker_path,
        {
            "schema": remote_cleanup.EVAL_CONTAINER_SCHEMA,
            "state": "pending",
            "task": task,
            "container_name": container_name,
            "container_id": "",
            "owner_nonce": owner_nonce,
            "owner_label": remote_cleanup.EVAL_OWNER_LABEL,
            "owner_schema_label": remote_cleanup.EVAL_SCHEMA_LABEL,
            "owner_schema": remote_cleanup.EVAL_SCHEMA_LABEL_VALUE,
            "cidfile": str(cidfile),
            "created_at": now(),
        },
    )
    docker_cmd = [
        "timeout",
        str(eval_timeout),
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"{remote_cleanup.EVAL_OWNER_LABEL}={owner_nonce}",
        "--label",
        f"{remote_cleanup.EVAL_SCHEMA_LABEL}={remote_cleanup.EVAL_SCHEMA_LABEL_VALUE}",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/bash",
        "--cidfile",
        str(cidfile),
        "-v",
        f"{input_dir}:/eval_input:ro",
        "-v",
        f"{output_dir}:/eval_output",
        image,
        "/eval_input/run_prolite_direct_eval.sh",
    ]
    append_jsonl(
        run_dir / "eval_attempts.jsonl",
        {
            "time": now(),
            "phase": "eval_attempt_started",
            "task": task,
            "record_id": row_record_id(prediction),
            "patch_sha256": row_patch_sha(prediction),
            "eval_spec_sha256": eval_spec_sha256,
        },
    )
    cleanup_quiesced = True
    container_cleanup = None
    with open_locked_append(command_log) as log:
        log.write(("\n===== eval start " + now() + " =====\n").encode())
        spawn_signal_state = block_spawn_signals()
        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                container_cleanup = clear_pending_eval_marker(
                    cidfile,
                    marker_path,
                    container_name,
                )
            log.write((f"failed to start eval container: {exc}\n").encode())
            docker_exit = 127
        except BaseException:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                clear_pending_eval_marker(
                    cidfile,
                    marker_path,
                    container_name,
                )
            raise
        else:
            ACTIVE_CHILD_PGIDS.add(proc.pid)
            binding = bind_eval_container_marker(
                cidfile,
                marker_path,
                container_name,
                proc,
            )
            if not binding.get("ok"):
                cleanup_quiesced = terminate_process_group_bounded(proc)
                cleanup = cleanup_eval_container(
                    cidfile,
                    marker_path,
                    container_name,
                )
                if cleanup_quiesced:
                    ACTIVE_CHILD_PGIDS.discard(proc.pid)
                summary = {
                    "schema": "opencollab.prolite_direct_eval.v2",
                    "status": "technical_eval_failed",
                    "task": task,
                    "resolved": False,
                    "patch_sha256": row_patch_sha(prediction),
                    "record_id": row_record_id(prediction),
                    "technical_reasons": ["container_identity_binding"],
                    "container_binding": binding,
                    "container_cleanup": cleanup,
                    "cleanup_quiesced": cleanup_quiesced,
                }
                write_json(summary_path, summary)
                return {"status": "technical_eval_failed", "task": task, "summary": summary}
            try:
                try:
                    restore_spawn_signals(spawn_signal_state)
                    docker_exit = proc.wait(timeout=eval_timeout + 120)
                    cleanup_quiesced = ensure_process_group_quiesced_after_wait(proc)
                    if not cleanup_quiesced:
                        docker_exit = PROCESS_CLEANUP_FAILED_EXIT_CODE
                except subprocess.TimeoutExpired:
                    log.write((f"outer eval timeout after {eval_timeout + 120}s\n").encode())
                    cleanup_quiesced = terminate_process_group_bounded(proc)
                    docker_exit = 124 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                except BaseException:
                    cleanup_quiesced = False
                    try:
                        cleanup_quiesced = terminate_process_group_bounded(proc)
                    except BaseException:
                        pass
                    try:
                        cleanup_eval_container(
                            cidfile,
                            marker_path,
                            container_name,
                        )
                    except BaseException:
                        pass
                    raise
            finally:
                if cleanup_quiesced:
                    ACTIVE_CHILD_PGIDS.discard(proc.pid)

    if container_cleanup is None:
        container_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            container_name,
        )

    artifacts = read_eval_output_artifacts(
        output_dir,
        f2p_plan,
        p2p_plan,
        proof_nonce,
    )
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=docker_exit,
        cleanup_quiesced=cleanup_quiesced,
        container_cleanup=container_cleanup,
    )
    output_artifact_errors = artifacts["output_artifact_errors"]
    base_commit_status = artifacts["base_commit_status"]
    service_status = artifacts["service_status"]
    before_status = artifacts["before_status"]
    post_before_base_status = artifacts["post_before_base_status"]
    model_status = artifacts["model_status"]
    test_status = artifacts["test_status"]
    f2p_status = artifacts["f2p_status"]
    p2p_status = artifacts["p2p_status"]
    f2p_log_tail = artifacts["f2p_log_tail"]
    p2p_log_tail = artifacts["p2p_log_tail"]
    f2p_evidence = artifacts["f2p_evidence"]
    p2p_evidence = artifacts["p2p_evidence"]
    f2p_command = artifacts["f2p_command"]
    p2p_command = artifacts["p2p_command"]
    base_commit_log_tail = artifacts["base_commit_log_tail"]
    before_repo_log_tail = artifacts["before_repo_log_tail"]
    service_bootstrap_log_tail = artifacts["service_bootstrap_log_tail"]
    model_patch_log_tail = artifacts["model_patch_log_tail"]
    test_patch_log_tail = artifacts["test_patch_log_tail"]
    technical_reasons = verdict["technical_reasons"]
    technical_error = verdict["technical_error"]
    resolved = verdict["resolved"]
    summary_status = verdict["summary_status"]
    report = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": summary_status,
        "instance_id": task,
        "resolved": resolved,
        "patch_successfully_applied": model_status == 0,
        "error": bool(technical_error),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "eval_spec_sha256": eval_spec_sha256,
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "tests_status": {
            "base_commit_status": base_commit_status,
            "service_bootstrap_status": service_status,
            "before_repo_status": before_status,
            "post_before_base_status": post_before_base_status,
            "model_patch_status": model_status,
            "test_patch_status": test_status,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": p2p_status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": f2p_evidence,
            "pass_to_pass_evidence": p2p_evidence,
            "f2p_command": f2p_command,
            "p2p_command": p2p_command,
            "base_commit_log_tail": base_commit_log_tail,
            "before_repo_log_tail": before_repo_log_tail,
            "service_bootstrap_log_tail": service_bootstrap_log_tail,
            "f2p_log_tail": f2p_log_tail,
            "p2p_log_tail": p2p_log_tail,
            "model_patch_log_tail": model_patch_log_tail,
            "test_patch_log_tail": test_patch_log_tail,
        },
    }
    write_json(report_path, {task: report})
    summary = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": summary_status,
        "task": task,
        "resolved": resolved,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "eval_spec_sha256": eval_spec_sha256,
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "report_path": str(report_path),
        "command_log": str(command_log),
        "tests_status": report["tests_status"],
    }
    write_json(summary_path, summary)
    return {
        "status": "eval_done" if not technical_error else "technical_eval_failed",
        "task": task,
        "summary": summary,
        "report_path": str(report_path),
        "executed": True,
    }


def eval_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    done, prediction, _metric, pairing = generation_done(
        run_dir,
        task,
        require_identity=not eval_only,
    )
    if not done:
        result = dict(eval_for_task_once(row))
        result["attempt_count"] = 0
        result["max_eval_attempts"] = max_eval_attempts
        return result
    persisted_attempts = eval_attempt_count(run_dir, prediction, task)
    if persisted_attempts >= max_eval_attempts:
        previous = load_json(run_dir / eval_dir_name / "summary.json")
        status = (
            "eval_done"
            if eval_summary_matches_prediction(previous, prediction, task)
            else "technical_eval_failed"
        )
        return {
            "status": status,
            "task": task,
            "summary": previous,
            "pairing": pairing,
            "executed": False,
            "retry_budget_exhausted": status != "eval_done",
            "attempt_count": persisted_attempts,
            "max_eval_attempts": max_eval_attempts,
        }
    attempts = []
    retry_statuses = {"technical_eval_failed", "blocked_missing_eval_image"}
    for _ in range(max_eval_attempts - persisted_attempts):
        result = dict(eval_for_task_once(row))
        attempts.append(result)
        if result.get("status") not in retry_statuses:
            break
        if not eval_retry_cleanup_safe(result):
            break
        current_attempts = eval_attempt_count(run_dir, prediction, task)
        if current_attempts >= max_eval_attempts:
            break
        append_jsonl(
            base_run_dir / "events.jsonl",
            {
                "time": now(),
                "phase": "eval_retry",
                "task": task,
                "attempt": current_attempts + 1,
                "previous_status": result.get("status"),
                "technical_reasons": (
                    result.get("summary", {}).get("technical_reasons", [])
                ),
            },
        )
    final = dict(attempts[-1])
    final["attempt_count"] = eval_attempt_count(run_dir, prediction, task)
    final["max_eval_attempts"] = max_eval_attempts
    if len(attempts) > 1:
        final["attempts"] = attempts
    return final


def write_markdown(summary):
    lines = [
        f"# SWE G1.1 Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- solver_attribution: `{summary['solver_attribution']}`",
        f"- llm_model: `{summary['llm_model']}`",
        f"- tasks: `{summary['counts']['tasks']}`",
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
        f"- resolved: `{summary['counts']['resolved']}`",
        f"- unresolved: `{summary['counts']['unresolved']}`",
        f"- technical_failed: `{summary['counts']['technical_failed']}`",
        "",
        "| idx | task | generation | eval | resolved | patch | report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        report = row.get("eval", {}).get("report_path") or ""
        patch_sha = (
            row.get("generation", {}).get("patch_sha256")
            or row.get("eval", {}).get("summary", {}).get("patch_sha256")
            or ""
        )
        lines.append(
            "| {idx} | `{task}` | `{gen}` | `{ev}` | `{resolved}` | `{patch}` | `{report}` |".format(
                idx=row["index"],
                task=row["task"],
                gen=row.get("generation", {}).get("status", ""),
                ev=row.get("eval", {}).get("status", ""),
                resolved=row.get("eval", {}).get("summary", {}).get("resolved", ""),
                patch=patch_sha[:12],
                report=report,
            )
        )
    summary["markdown"] = "\n".join(lines) + "\n"


def main():
    config_errors = validate_runner_config()
    if config_errors:
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "invalid_config",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "config_errors": config_errors,
            "counts": {
                "tasks": 0,
                "generation_done": 0,
                "empty_patch": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
            },
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    preflight = {
        "dataset_exists": dataset_path.exists(),
        "remote_root_exists": remote_root.exists(),
        "remote_repo_exists": remote_repo.exists(),
        "remote_runtime_required": not eval_only,
        "proxy_health": (
            {"ok": True, "status": "skipped_eval_only"}
            if eval_only
            else http_health(remote_proxy_base_url + "/healthz", timeout=45)
        ),
    }
    if not all(
        [
            preflight["dataset_exists"],
            preflight["remote_root_exists"],
            preflight["remote_repo_exists"] or not preflight["remote_runtime_required"],
            preflight["proxy_health"].get("ok"),
        ]
    ):
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "preflight_failed",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "preflight": preflight,
            "counts": {
                "tasks": 0,
                "generation_done": 0,
                "empty_patch": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
            },
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    selected = load_dataset(start_index, limit)
    base_run_dir.mkdir(parents=True, exist_ok=True)
    result_rows = []
    for offset, row in enumerate(selected, start_index):
        task = row["instance_id"]
        if eval_only:
            run_dir = base_run_dir / task
            done, prediction, metric, pairing = generation_done(
                run_dir,
                task,
                require_identity=False,
            )
            if done:
                gen = generation_done_result(
                    task,
                    prediction,
                    metric,
                    pairing,
                    eval_only=True,
                    artifact_identity_status=historical_generation_identity_status(
                        prediction,
                        metric,
                        task,
                    ),
                )
            else:
                gen = {
                    "status": "skipped_no_generation_patch",
                    "task": task,
                    "pairing": pairing,
                    "eval_only": True,
                }
            generation_phase = "generation_observed"
        else:
            gen = generation_for_task(row)
            generation_phase = "generation"
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": generation_phase, "task": task, "result": gen})
        if gen.get("status") == "empty_patch":
            ev = {
                "status": "skipped_empty_patch",
                "task": task,
                "pairing": gen.get("pairing"),
                "attempt_count": 0,
                "max_eval_attempts": max_eval_attempts,
            }
        elif dry_run and gen.get("status") in {"would_generate", "generation_done"}:
            ev = {"status": "would_eval", "task": task}
        elif gen.get("status") == "generation_done":
            ev = eval_for_task(row)
        else:
            ev = {
                "status": "skipped_generation_not_ready",
                "task": task,
                "generation_status": gen.get("status"),
                "reason": "generation_not_ready",
            }
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "eval", "task": task, "result": ev})
        result_rows.append({"index": offset, "task": task, "generation": gen, "eval": ev})
    generation_ok_statuses = {"generation_done", "empty_patch"}
    eval_ok_statuses = {"eval_done", "skipped_empty_patch"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "empty_patch": sum(1 for row in result_rows if row["generation"].get("status") == "empty_patch"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
        **eval_attempt_summary(result_rows),
        "resolved": sum(1 for row in result_rows if row["eval"].get("summary", {}).get("resolved") is True),
        "unresolved": sum(
            1
            for row in result_rows
            if row["eval"].get("status") == "eval_done" and row["eval"].get("summary", {}).get("resolved") is False
        ),
        "technical_failed": sum(
            1
            for row in result_rows
            if row["generation"].get("status") not in generation_ok_statuses
            or row["eval"].get("status") not in eval_ok_statuses
        ),
    }
    status = "done" if counts["technical_failed"] == 0 else "done_with_technical_failures"
    if dry_run and counts["technical_failed"] == 0:
        status = "dry_run"
    summary = {
        "schema": "opencollab.swe_g11_prolite_runner.v1",
        "status": status,
        "generated_at": now(),
        "slice": slice_label(),
        "base_run_dir": str(base_run_dir),
        "remote_runtime_repo": str(remote_repo),
        "workflow": workflow,
        "workflow_env": workflow_env,
        "openhands_command_sha256": openhands_command_sha256,
        "openhands_empty_patch_rejections": openhands_empty_patch_rejections,
        "max_empty_patch_retries": max_empty_patch_retries,
        "model_name": model_name,
        "llm_model": llm_model,
        "llm_provider": llm_provider,
        "context_window": context_window,
        "temperature": temperature,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
        "invocation_id": invocation_id,
        "budget": budget,
        "max_steps": max_steps,
        "max_task_starts": max_task_starts,
        "max_eval_attempts": max_eval_attempts,
        "eval_only": eval_only,
        "eval_dir_name": eval_dir_name,
        "solver_attribution": (
            "historical_artifact" if eval_only else "current_run"
        ),
        "preflight": preflight,
        "counts": counts,
        "rows": result_rows,
    }
    write_markdown(summary)
    write_json(base_run_dir / "summary.json", summary)
    atomic_write_bytes(
        base_run_dir / "summary.md",
        summary["markdown"].encode("utf-8"),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if counts["technical_failed"] == 0 else 1


__all__ = [name for name in globals() if not name.startswith("__")]
