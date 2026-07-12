"""Bounded Pro-Lite artifact snapshots and pure verdict derivation."""

from __future__ import annotations

import os
import re

from opencollab.harness.swe_v1_remote_commands import _plan_log_proof_matches
from opencollab.harness.swe_v1_remote_core import (
    RecordInputFormatError,
    RecordInputLimitError,
)
from opencollab.harness.swe_v1_remote_generation import eval_log_has_infra_failure
from opencollab.harness.swe_v1_remote_records import read_tail_text
from opencollab.harness.swe_v1_remote_state import (
    MAX_EXIT_STATUS_BYTES,
    MAX_LOG_TAIL_BYTES,
    MAX_TEST_EVIDENCE_BYTES,
    open_regular_binary,
)


def _read_exit(output_dir, errors, name, default=99):
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
        errors.append(f"missing:{name}")
        return default
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return default


def _read_text(output_dir, errors, name, limit=4000):
    try:
        return read_tail_text(output_dir / name, limit)
    except OSError as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return ""


def _read_required_text(output_dir, errors, name, limit=4000):
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
        errors.append(f"missing:{name}")
        return ""
    except (OSError, ValueError) as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return ""


def _read_plan_evidence(output_dir, errors, prefix, plan, proof_nonce):
    evidence = []
    for index, expected_command in enumerate(plan["commands"], 1):
        stem = f"{prefix}.batch_{index:03d}"
        error_count = len(errors)
        status = _read_exit(output_dir, errors, f"{stem}.exit")
        observed_command = _read_text(
            output_dir,
            errors,
            f"{stem}.command",
            MAX_LOG_TAIL_BYTES,
        ).rstrip("\n")
        log_error_count = len(errors)
        log_text = _read_required_text(
            output_dir,
            errors,
            f"{stem}.log",
            MAX_TEST_EVIDENCE_BYTES,
        )
        log_artifact_safe = len(errors) == log_error_count
        proofs = plan.get("proofs") or []
        proof = proofs[index - 1] if index <= len(proofs) else None
        proof_text = ""
        if isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports":
            proof_text = _read_required_text(
                output_dir,
                errors,
                f"{stem}.proof.{proof_nonce}.jsonl",
                MAX_TEST_EVIDENCE_BYTES,
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
                "artifact_safe": len(errors) == error_count,
            }
        )
    return evidence


def _plan_evidence_complete(plan, evidence):
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


def read_eval_output_artifacts(output_dir, f2p_plan, p2p_plan, proof_nonce):
    """Read every verdict-relevant artifact before returning one snapshot."""
    errors = []
    base_commit_status = _read_exit(output_dir, errors, "base_commit.exit")
    service_status = _read_exit(output_dir, errors, "service_bootstrap.exit", 0)
    before_status = _read_exit(output_dir, errors, "before_repo.exit")
    post_before_base_status = _read_exit(
        output_dir,
        errors,
        "post_before_base.exit",
    )
    model_status = _read_exit(output_dir, errors, "model_patch.exit")
    test_status = _read_exit(output_dir, errors, "test_patch.exit")
    f2p_status = _read_exit(output_dir, errors, "f2p.exit")
    p2p_status = _read_exit(output_dir, errors, "p2p.exit", 0)
    f2p_log_tail = _read_text(output_dir, errors, "f2p.log")
    p2p_log_tail = _read_text(output_dir, errors, "p2p.log")
    f2p_evidence = _read_plan_evidence(
        output_dir,
        errors,
        "f2p",
        f2p_plan,
        proof_nonce,
    )
    p2p_evidence = (
        _read_plan_evidence(
            output_dir,
            errors,
            "p2p",
            p2p_plan,
            proof_nonce,
        )
        if p2p_plan["commands"]
        else []
    )
    return {
        "output_artifact_errors": errors,
        "base_commit_status": base_commit_status,
        "service_status": service_status,
        "before_status": before_status,
        "post_before_base_status": post_before_base_status,
        "model_status": model_status,
        "test_status": test_status,
        "f2p_status": f2p_status,
        "p2p_status": p2p_status,
        "f2p_log_tail": f2p_log_tail,
        "p2p_log_tail": p2p_log_tail,
        "f2p_evidence": f2p_evidence,
        "p2p_evidence": p2p_evidence,
        "f2p_evidence_complete": _plan_evidence_complete(f2p_plan, f2p_evidence),
        "p2p_evidence_complete": not p2p_plan["commands"]
        or _plan_evidence_complete(p2p_plan, p2p_evidence),
        "f2p_command": _read_text(output_dir, errors, "f2p.command", 1000),
        "p2p_command": _read_text(output_dir, errors, "p2p.command", 1000),
        "service_bootstrap_log_tail": _read_text(
            output_dir,
            errors,
            "service_bootstrap.log",
        ),
        "base_commit_log_tail": _read_text(output_dir, errors, "base_commit.log"),
        "before_repo_log_tail": _read_text(output_dir, errors, "before_repo.log"),
        "model_patch_log_tail": _read_text(output_dir, errors, "model_patch.log"),
        "test_patch_log_tail": _read_text(output_dir, errors, "test_patch.log"),
    }


def _plan_evidence_mismatch(artifacts, prefix):
    evidence = artifacts[f"{prefix}_evidence"]
    status = artifacts[f"{prefix}_status"]
    aggregate_status = next(
        (item["status"] for item in evidence if item["status"] != 0),
        0,
    )
    return not artifacts[f"{prefix}_evidence_complete"] or bool(
        evidence and aggregate_status != status
    )


def derive_eval_verdict(artifacts, *, docker_exit, cleanup_quiesced, container_cleanup):
    """Derive a verdict only from an already complete artifact snapshot."""
    f2p_evidence = artifacts["f2p_evidence"]
    p2p_evidence = artifacts["p2p_evidence"]
    f2p_status = artifacts["f2p_status"]
    p2p_status = artifacts["p2p_status"]
    reason_checks = (
        ("unsafe_or_missing_output_artifact", bool(artifacts["output_artifact_errors"])),
        ("fail_to_pass_evidence", _plan_evidence_mismatch(artifacts, "f2p")),
        ("pass_to_pass_evidence", _plan_evidence_mismatch(artifacts, "p2p")),
        ("docker_exit", docker_exit != 0),
        ("process_cleanup", not cleanup_quiesced),
        ("container_cleanup", not container_cleanup.get("ok")),
        ("base_commit", artifacts["base_commit_status"] != 0),
        ("service_bootstrap", artifacts["service_status"] != 0),
        ("before_repo", artifacts["before_status"] != 0),
        (
            "post_before_base_commit",
            artifacts["post_before_base_status"] != 0,
        ),
        ("model_patch", artifacts["model_status"] != 0),
        ("test_patch", artifacts["test_status"] != 0),
        (
            "fail_to_pass_infra",
            eval_log_has_infra_failure(f2p_status, artifacts["f2p_log_tail"]),
        ),
        (
            "pass_to_pass_infra",
            eval_log_has_infra_failure(p2p_status, artifacts["p2p_log_tail"]),
        ),
    )
    technical_reasons = [reason for reason, active in reason_checks if active]
    technical_error = bool(technical_reasons)
    resolved = bool(
        not technical_error
        and f2p_status == 0
        and p2p_status == 0
        and all(item["status"] == 0 for item in f2p_evidence)
        and all(item["status"] == 0 for item in p2p_evidence)
    )
    return {
        "technical_reasons": technical_reasons,
        "technical_error": technical_error,
        "resolved": resolved,
        "summary_status": "technical_eval_failed" if technical_error else "done",
    }


__all__ = ["derive_eval_verdict", "read_eval_output_artifacts"]
