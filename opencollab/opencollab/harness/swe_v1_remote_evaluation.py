"""Official evaluation execution, summary, and remote CLI logic."""

# ruff: noqa: E501, F403, F405

from opencollab.harness import swe_v1_remote_cleanup as remote_cleanup
from opencollab.harness.swe_v1_remote_commands import *
from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_generation import *
from opencollab.harness.swe_v1_remote_records import *
from opencollab.harness.swe_v1_remote_state import *


def eval_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    eval_dir = run_dir / "official_eval_v1_prolite26_35_20260707"
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    done, prediction, metric, pairing = generation_done(run_dir, task)
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
    f2p_plan = prolite_test_plan(row, fail_to_pass)
    p2p_plan = prolite_test_plan(row, pass_to_pass)
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
        return {"status": "would_eval", "task": task}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_eval_image", "task": task, "image_status": image_status}
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
    atomic_write_bytes(input_dir / "f2p.command", (f2p_cmd + "\n").encode("utf-8"))
    atomic_write_bytes(input_dir / "p2p.command", (p2p_cmd + "\n").encode("utf-8"))
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
    inner = """#!/usr/bin/env bash
set +e
cd /app 2>/dev/null || cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || cd /
export PATH="/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:/root/go/bin:/usr/local/node/bin:/opt/node/bin:/root/.local/share/pnpm:/root/.npm-global/bin:/app/node_modules/.bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1 && command -v corepack >/dev/null 2>&1; then
  corepack enable >/tmp/prolite_corepack.log 2>&1 || true
fi
bash /eval_input/service_bootstrap.sh > /eval_output/service_bootstrap.log 2>&1
echo "$?" > /eval_output/service_bootstrap.exit
bash /eval_input/before_repo.sh > /eval_output/before_repo.log 2>&1
echo "$?" > /eval_output/before_repo.exit
model_status=0
if [ -s /eval_input/model.patch ]; then
  git apply --whitespace=nowarn /eval_input/model.patch > /eval_output/model_patch.log 2>&1
  model_status=$?
  if [ "$model_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/model.patch >> /eval_output/model_patch.log 2>&1
    model_status=$?
  fi
fi
echo "$model_status" > /eval_output/model_patch.exit
test_status=0
if [ "$model_status" -eq 0 ] && [ -s /eval_input/test.patch ]; then
  git apply --whitespace=nowarn /eval_input/test.patch > /eval_output/test_patch.log 2>&1
  test_status=$?
  if [ "$test_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/test.patch >> /eval_output/test_patch.log 2>&1
    test_status=$?
  fi
fi
echo "$test_status" > /eval_output/test_patch.exit
if [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
  cp /eval_input/f2p.command /eval_output/f2p.command
  bash /eval_input/f2p.sh > /eval_output/f2p.log 2>&1
  echo "$?" > /eval_output/f2p.exit
  cp /eval_input/p2p.command /eval_output/p2p.command
  bash /eval_input/p2p.sh > /eval_output/p2p.log 2>&1
  echo "$?" > /eval_output/p2p.exit
else
  echo 99 > /eval_output/f2p.exit
  echo 99 > /eval_output/p2p.exit
fi
exit 0
"""
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

    output_artifact_errors = []

    def read_exit(name, default=99):
        path = output_dir / name
        try:
            with open_regular_binary(path) as handle:
                opened = os.fstat(handle.fileno())
                if opened.st_size > MAX_EXIT_STATUS_BYTES:
                    raise RecordInputLimitError(f"exit status exceeds byte limit: {path}")
                raw = handle.read(MAX_EXIT_STATUS_BYTES + 1)
            text = raw.decode("ascii").strip()
            if not re.fullmatch(r"-?[0-9]+", text):
                raise RecordInputFormatError(f"invalid exit status: {path}")
            return int(text)
        except FileNotFoundError:
            output_artifact_errors.append(f"missing:{name}")
            return default
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            output_artifact_errors.append(f"unsafe:{name}:{type(exc).__name__}")
            return default

    def read_text(name, limit=4000):
        try:
            return read_tail_text(output_dir / name, limit)
        except OSError as exc:
            output_artifact_errors.append(f"unsafe:{name}:{type(exc).__name__}")
            return ""

    def read_required_text(name, limit=4000):
        path = output_dir / name
        try:
            with open_regular_binary(path) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > limit:
                    raise RecordInputLimitError(
                        f"required output artifact exceeds byte limit: {path}"
                    )
                return handle.read(limit + 1).decode("utf-8", errors="replace")
        except FileNotFoundError:
            output_artifact_errors.append(f"missing:{name}")
            return ""
        except (OSError, ValueError) as exc:
            output_artifact_errors.append(f"unsafe:{name}:{type(exc).__name__}")
            return ""

    def read_plan_evidence(prefix, plan):
        evidence = []
        for index, expected_command in enumerate(plan["commands"], 1):
            stem = f"{prefix}.batch_{index:03d}"
            error_count = len(output_artifact_errors)
            status = read_exit(f"{stem}.exit")
            observed_command = read_text(f"{stem}.command", MAX_LOG_TAIL_BYTES).rstrip("\n")
            log_error_count = len(output_artifact_errors)
            log_text = read_required_text(f"{stem}.log", MAX_LOG_TAIL_BYTES)
            log_artifact_safe = len(output_artifact_errors) == log_error_count
            proofs = plan.get("proofs") or []
            proof = proofs[index - 1] if index <= len(proofs) else None
            proof_text = ""
            if isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports":
                proof_text = read_required_text(
                    f"{stem}.proof.{proof_nonce}.jsonl",
                    MAX_LOG_TAIL_BYTES,
                )
            evidence.append(
                {
                    "batch": index,
                    "status": status,
                    "command_matches_plan": observed_command == expected_command,
                    "log_artifact_safe": log_artifact_safe,
                    "target_proof_matches_plan": _plan_log_proof_matches(
                        proof,
                        log_text,
                        proof_text,
                    ),
                    "artifact_safe": len(output_artifact_errors) == error_count,
                }
            )
        return evidence

    def plan_evidence_complete(plan, evidence):
        return (
            bool(plan["commands"])
            and len(evidence) == len(plan["commands"])
            and all(
                item["command_matches_plan"]
                and item["log_artifact_safe"]
                and item["target_proof_matches_plan"]
                and item["artifact_safe"]
                for item in evidence
            )
        )

    def aggregate_plan_status(evidence):
        return next((item["status"] for item in evidence if item["status"] != 0), 0)

    service_status = read_exit("service_bootstrap.exit", 0)
    before_status = read_exit("before_repo.exit")
    model_status = read_exit("model_patch.exit")
    test_status = read_exit("test_patch.exit")
    f2p_status = read_exit("f2p.exit")
    p2p_status = read_exit("p2p.exit", 0)
    f2p_log_tail = read_text("f2p.log")
    p2p_log_tail = read_text("p2p.log")
    f2p_evidence = read_plan_evidence("f2p", f2p_plan)
    p2p_evidence = read_plan_evidence("p2p", p2p_plan) if p2p_plan["commands"] else []
    f2p_evidence_complete = plan_evidence_complete(f2p_plan, f2p_evidence)
    p2p_evidence_complete = not p2p_plan["commands"] or plan_evidence_complete(p2p_plan, p2p_evidence)
    technical_reasons = []
    if output_artifact_errors:
        technical_reasons.append("unsafe_or_missing_output_artifact")
    if not f2p_evidence_complete or aggregate_plan_status(f2p_evidence) != f2p_status:
        technical_reasons.append("fail_to_pass_evidence")
    if not p2p_evidence_complete or (p2p_evidence and aggregate_plan_status(p2p_evidence) != p2p_status):
        technical_reasons.append("pass_to_pass_evidence")
    if docker_exit != 0:
        technical_reasons.append("docker_exit")
    if not cleanup_quiesced:
        technical_reasons.append("process_cleanup")
    if not container_cleanup.get("ok"):
        technical_reasons.append("container_cleanup")
    if service_status != 0:
        technical_reasons.append("service_bootstrap")
    if before_status != 0:
        technical_reasons.append("before_repo")
    if model_status != 0:
        technical_reasons.append("model_patch")
    if test_status != 0:
        technical_reasons.append("test_patch")
    if eval_log_has_infra_failure(f2p_status, f2p_log_tail):
        technical_reasons.append("fail_to_pass_infra")
    if eval_log_has_infra_failure(p2p_status, p2p_log_tail):
        technical_reasons.append("pass_to_pass_infra")
    technical_error = bool(technical_reasons)
    resolved = bool(
        not technical_error
        and f2p_status == 0
        and p2p_status == 0
        and all(item["status"] == 0 for item in f2p_evidence)
        and all(item["status"] == 0 for item in p2p_evidence)
    )
    summary_status = "technical_eval_failed" if technical_error else "done"
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
            "service_bootstrap_status": service_status,
            "before_repo_status": before_status,
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
            "f2p_command": read_text("f2p.command", 1000),
            "p2p_command": read_text("p2p.command", 1000),
            "service_bootstrap_log_tail": read_text("service_bootstrap.log"),
            "f2p_log_tail": f2p_log_tail,
            "p2p_log_tail": p2p_log_tail,
            "model_patch_log_tail": read_text("model_patch.log"),
            "test_patch_log_tail": read_text("test_patch.log"),
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
    }


def write_markdown(summary):
    lines = [
        f"# SWE G1.1 Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
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
                "eval_done": 0,
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
        "proxy_health": http_health(remote_proxy_base_url + "/healthz", timeout=45),
    }
    if not all(
        [
            preflight["dataset_exists"],
            preflight["remote_root_exists"],
            preflight["remote_repo_exists"],
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
                "eval_done": 0,
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
        gen = generation_for_task(row)
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "generation", "task": task, "result": gen})
        if dry_run and gen.get("status") in {"would_generate", "generation_done"}:
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
    generation_ok_statuses = {"generation_done"}
    eval_ok_statuses = {"eval_done"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
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
        "model_name": model_name,
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


def validate_runner_config():
    errors = []
    if start_index < 1:
        errors.append("start_index must be >= 1")
    if limit <= 0:
        errors.append("limit must be > 0")
    if limit > MAX_TASKS_PER_RUN:
        errors.append(f"limit must be <= {MAX_TASKS_PER_RUN}")
    if max_task_starts < 0:
        errors.append("max_task_starts must be >= 0")
    return errors


__all__ = [name for name in globals() if not name.startswith("__")]
