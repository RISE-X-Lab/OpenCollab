from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from generation_proof_test_support import trusted_patch_proof_fields
from opencollab.harness import swe_v1_remote_records as remote_records
from test_swe_g11_parallel_runner import (
    _args,
    _load_module,
    _reusable_summary,
)


def _task_summary(config, status: str = "done") -> dict:
    summary = _reusable_summary(config, 51)
    summary["status"] = status
    return summary


def _fact_report(indices: tuple[int, ...], *, technical: tuple[int, ...] = ()) -> dict:
    tasks = [
        {
            "index": index,
            "task": f"task-{index}",
            "generation_status": "generation_failed" if index in technical else "generation_done",
            "eval_attempt_count": 0 if index in technical else 1,
            "eval_success": index not in technical,
            "eval_pending": False,
            "resolved": None if index in technical else True,
            "technical_failed": index in technical,
        }
        for index in indices
    ]
    successful = len(indices) - len(technical)
    return {
        "schema": "opencollab.swe_eval_layer_final_report.v1",
        "expected_indices": list(indices),
        "counts": {
            "tasks": len(indices),
            "eval_attempts": successful,
            "eval_retry_tasks": 0,
            "eval_success": successful,
            "empty_patch": 0,
            "eval_pending": 0,
            "resolved": successful,
            "unresolved": 0,
            "technical_failed_final": len(technical),
        },
        "tasks": tasks,
    }


def test_task_result_accepts_only_a_strict_terminal_summary(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    valid = module.task_result_from_summary(
        config,
        51,
        _task_summary(config),
        reused=False,
        elapsed=1.0,
        process_returncode=0,
    )
    assert valid["completed"] is True
    assert valid["summary_validation_reasons"] == []

    cases = {
        "running": _task_summary(config, "running"),
        "empty_counts": {**_task_summary(config), "counts": {}},
        "wrong_schema": {**_task_summary(config), "schema": "legacy"},
    }
    for name, summary in cases.items():
        result = module.task_result_from_summary(
            config,
            51,
            summary,
            reused=False,
            elapsed=1.0,
            process_returncode=0,
        )
        assert result["completed"] is False, name
        assert result["summary_validation_reasons"], name


def test_task_result_rejects_returncode_status_conflict(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    result = module.task_result_from_summary(
        config,
        51,
        _task_summary(config),
        reused=False,
        elapsed=1.0,
        process_returncode=1,
    )

    assert result["completed"] is False
    assert "returncode_status_conflict" in result["summary_validation_reasons"]


def test_strict_technical_terminal_is_completed_without_contributing_a_verdict(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    summary = _reusable_summary(config, 51)
    summary["status"] = "done_with_technical_failures"
    summary["counts"].update(eval_done=0, resolved=0, technical_failed=1)
    evaluation = summary["rows"][0]["eval"]
    evaluation["status"] = "technical_eval_failed"
    evaluation["summary"].update(
        status="technical_eval_failed",
        resolved=False,
        technical_reasons=["fail_to_pass_infra"],
    )

    result = module.task_result_from_summary(
        config,
        51,
        summary,
        reused=False,
        elapsed=1.0,
        process_returncode=1,
    )

    assert result["completed"] is True
    assert result["resolved"] == 0
    assert result["unresolved"] == 0
    assert result["technical_failed"] == 1
    assert result["summary_validation_reasons"] == []


def test_build_eval_fact_report_passes_the_trusted_config_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            indices="3,7,9",
            start_index=None,
            end_index=None,
        )
    )
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((3, 7, 9)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "done"
    expected_values = [
        captured[index + 1]
        for index, value in enumerate(captured)
        if value == "--expected-index"
    ]
    assert expected_values == ["3", "7", "9"]


def test_build_eval_fact_report_rejects_a_wrong_output_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="3,7", start_index=None, end_index=None)
    )

    def fake_run(command, **kwargs):
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((3,)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "invalid_fact_report"


def test_build_eval_fact_report_does_not_call_a_technical_row_done(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    def fake_run(command, **kwargs):
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((51,), technical=(51,)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "done_with_technical_failures"
    assert result["counts"]["resolved"] == 0
    assert result["counts"]["technical_failed_final"] == 1


def test_aggregate_cannot_finish_when_the_fact_report_census_is_invalid(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="1"))
    results = [
        {
            "index": 1,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
        }
    ]

    summary = module.aggregate(
        config,
        results,
        fact_report={"status": "invalid_census"},
    )

    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["technical_failed"] == 1


def test_fresh_success_without_identity_or_direct_proof_cannot_reach_top_level_done(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    weak = _reusable_summary(config, 51)
    weak["rows"][0]["generation"].pop("record_id")
    weak["rows"][0]["eval"]["summary"].pop("tests_status")

    result = module.task_result_from_summary(
        config,
        51,
        weak,
        reused=False,
        elapsed=1.0,
        process_returncode=0,
    )
    summary = module.aggregate(config, [result])

    assert result["completed"] is False
    assert result["resolved"] == 0
    assert "missing_generation_record_id" in result["summary_validation_reasons"]
    assert "missing_direct_execution_proof" in result["summary_validation_reasons"]
    assert summary["status"] == "running"
    assert summary["counts"]["resolved"] == 0


def test_fact_report_with_a_technical_row_cannot_claim_done(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    report = _fact_report((51,), technical=(51,))

    status, counts, reasons = module._reports._validate_fact_report(report, config)

    assert status == "done_with_technical_failures"
    assert counts["technical_failed_final"] == 1
    assert reasons == []


def test_parallel_and_fact_count_conflict_becomes_technical_and_clears_resolved(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    results = [
        {
            "index": 51,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "empty_patch": 0,
            "eval_done": 1,
            "eval_attempts": 1,
            "eval_retry_tasks": 0,
            "resolved": 1,
            "unresolved": 0,
            "technical_failed": 0,
        }
    ]
    fact_report = {
        "status": "done",
        "counts": {
            **_fact_report((51,))["counts"],
            "resolved": 0,
            "unresolved": 1,
        },
    }

    summary = module.aggregate(config, results, fact_report=fact_report)

    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["unresolved"] == 0
    assert summary["counts"]["technical_failed"] == 1
    assert "parallel_fact_count_mismatch:resolved" in summary[
        "fact_report_validation_reasons"
    ]


def test_aggregate_rejects_duplicate_indices_as_a_complete_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="1,2", start_index=None, end_index=None)
    )
    results = [
        {"index": 1, "completed": True, "returncode": 0, "tasks": 1},
        {"index": 1, "completed": True, "returncode": 0, "tasks": 1},
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "running"


def test_report_reuse_accepts_only_a_production_proven_empty_patch(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    task = "task-51"
    record_id = "record-task-51"
    metric = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
        "workflow_status": "empty_patch_after_done",
        "runner_returncode": 1,
        "submission_eligible": False,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": False,
        **trusted_patch_proof_fields(""),
    }
    prediction = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
        "model_patch": "",
        "workflow_metric": metric,
    }
    generation = remote_records.empty_patch_result(
        task, prediction, metric, "record_id"
    )
    summary = _reusable_summary(config, 51)
    summary["counts"].update(
        generation_done=0,
        empty_patch=1,
        eval_done=0,
        eval_attempts=0,
        resolved=0,
    )
    summary["rows"] = [
        {
            "index": 51,
            "task": task,
            "generation": generation,
            "eval": {"status": "skipped_empty_patch", "task": task},
        }
    ]

    assert module.report_is_reusable(summary, config, 51) is True

    missing_evidence = json.loads(json.dumps(summary))
    missing_evidence["rows"][0]["generation"].pop("execution_quiesced")
    assert module.report_is_reusable(missing_evidence, config, 51) is False

    forged = {**summary, "rows": [dict(summary["rows"][0])]}
    forged["rows"][0]["generation"] = {
        "status": "empty_patch",
        "patch_len": 0,
        "workflow_status": "empty_patch_after_done",
    }
    assert module.report_is_reusable(forged, config, 51) is False


def test_parser_has_no_private_host_proxy_or_env_file_defaults(monkeypatch):
    for name in (
        "OPENCOLLAB_SWE_HOST",
        "OPENCOLLAB_SWE_LLM_MODEL",
        "OPENCOLLAB_REMOTE_PROXY_BASE_URL",
        "OPENCOLLAB_LOCAL_PROXY_BASE_URL",
        "OPENCOLLAB_PROXY_ENV_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    module = _load_module()

    args = module.build_parser().parse_args(["--indices", "1"])

    assert args.host == ""
    assert args.llm_model == ""
    assert args.remote_proxy_base_url == ""
    assert args.local_proxy_base_url == ""
    assert args.proxy_env_file is None


def test_run_one_does_not_rewrite_a_weak_legacy_empty_patch_report(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="51", runner_attempts=1)
    )
    report_path = module.task_paths(config, 51)["json_report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "status": "done_with_technical_failures",
        "counts": {"tasks": 1, "technical_failed": 1},
        "rows": [
            {
                "index": 51,
                "task": "task-51",
                "generation": {
                    "status": "generation_failed",
                    "workflow_status": "empty_patch_after_done",
                    "patch_len": 0,
                },
                "eval": {"status": "skipped_no_generation_patch"},
            }
        ],
    }
    original_text = json.dumps(legacy)
    report_path.write_text(original_text, encoding="utf-8")
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        assert report_path.read_text(encoding="utf-8") == original_text
        module.write_json(report_path, _reusable_summary(config, 51))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_one(config, 51)
    finally:
        module.subprocess.run = original_run

    assert calls == 1
    assert result["completed"] is True
    assert result["reused_existing_report"] is False
