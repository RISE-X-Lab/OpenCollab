from __future__ import annotations

import json

from test_swe_eval_layer_report import (
    _as_verified_empty,
    _load_module,
    _row,
    _write_json,
)


def test_expected_census_emits_rows_for_missing_duplicate_and_orchestrator_failure(tmp_path):
    module = _load_module()
    valid = _row(1, "task-1", "/run/task-1.log", 10, "eval_done", True)
    duplicate = _row(3, "task-3", "/run/task-3.log", 10, "eval_done", False)
    source = _write_json(
        tmp_path / "parallel.json",
        {
            "indices": [1, 2, 3, 4],
            "results": [
                {"index": 1, "completed": True, "runner_status": "done", "rows": [valid]},
                {"index": 3, "completed": True, "runner_status": "done", "rows": [duplicate]},
                {"index": 3, "completed": True, "runner_status": "done", "rows": [duplicate]},
                {
                    "index": 4,
                    "completed": False,
                    "runner_status": "orchestrator_exception",
                    "rows": [],
                },
            ],
        },
    )

    report = module.build_report([source], expected_indices=(1, 2, 3, 4))

    assert report["counts"]["tasks"] == 4
    assert report["counts"]["resolved"] == 1
    assert report["counts"]["technical_failed_final"] == 3
    by_index = {task["index"]: task for task in report["tasks"]}
    assert "missing_expected_task" in by_index[2]["technical_reasons"]
    assert "duplicate_orchestrator_result" in by_index[3]["technical_reasons"]
    assert "orchestrator_exception" in by_index[4]["technical_reasons"]


def test_explicit_duplicate_expected_index_is_a_technical_census_error(tmp_path):
    module = _load_module()
    source = _write_json(
        tmp_path / "source.json",
        {"rows": [_row(1, "task-1", "/run/task-1.log", 10, "eval_done", True)]},
    )

    report = module.build_report([source], expected_indices=(1, 1))

    assert report["expected_indices"] == [1]
    assert report["counts"]["tasks"] == 1
    assert report["counts"]["resolved"] == 0
    assert report["counts"]["technical_failed_final"] == 1
    assert "duplicate_expected_index" in report["tasks"][0]["technical_reasons"]


def test_cli_preserves_duplicate_expected_index_as_technical_evidence(
    tmp_path, monkeypatch
):
    module = _load_module()
    source = _write_json(
        tmp_path / "source.json",
        {"rows": [_row(1, "task-1", "/run/task-1.log", 10, "eval_done", True)]},
    )
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "swe_eval_layer_report.py",
            "--report-json",
            str(source),
            "--expected-index",
            "1",
            "--expected-index",
            "1",
            "--json-output",
            str(output),
            "--markdown-output",
            str(markdown),
        ],
    )

    assert module.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["counts"]["technical_failed_final"] == 1
    assert "duplicate_expected_index" in report["tasks"][0]["technical_reasons"]


def test_expected_census_marks_task_index_mapping_conflicts_technical(tmp_path):
    module = _load_module()
    source = _write_json(
        tmp_path / "mapping.json",
        {
            "indices": [1, 2],
            "rows": [
                _row(1, "same-task", "/run/one.log", 10, "eval_done", True),
                _row(2, "same-task", "/run/two.log", 10, "eval_done", True),
            ],
        },
    )

    report = module.build_report([source], expected_indices=(1, 2))

    assert report["counts"]["tasks"] == 2
    assert report["counts"]["technical_failed_final"] == 2
    assert all(
        "task_index_mapping_conflict" in task["technical_reasons"]
        for task in report["tasks"]
    )


def test_conflicting_terminal_verdict_is_deterministic_across_input_order(tmp_path):
    module = _load_module()
    passed = _write_json(
        tmp_path / "a-passed.json",
        {"rows": [_row(1, "task", "/run/task.log", 10, "eval_done", True)]},
    )
    failed = _write_json(
        tmp_path / "b-failed.json",
        {"rows": [_row(1, "task", "/run/task.log", 10, "eval_done", False)]},
    )

    forward = module.build_report([passed, failed])["tasks"][0]
    reverse = module.build_report([failed, passed])["tasks"][0]

    for key in ("eval_status", "resolved", "technical_failed", "technical_reasons", "attempts"):
        assert forward[key] == reverse[key]
    assert forward["resolved"] is None
    assert "conflicting_eval_verdicts" in forward["technical_reasons"]


def test_single_report_without_record_identity_is_technical(tmp_path):
    module = _load_module()
    row = _row(1, "task", "/run/task.log", 10, "eval_done", True)
    del row["generation"]["record_id"]
    source = _write_json(tmp_path / "missing-record.json", {"rows": [row]})

    report = module.build_report([source])

    assert report["counts"]["technical_failed_final"] == 1
    assert report["tasks"][0]["resolved"] is None
    assert "missing_generation_record_id" in report["tasks"][0]["technical_reasons"]


def test_persisted_direct_summary_remains_valid_when_current_invocation_did_not_execute(tmp_path):
    module = _load_module()
    row = _row(1, "task", "/run/task.log", 10, "eval_done", True)
    row["eval"]["executed"] = False
    source = _write_json(tmp_path / "persisted.json", {"rows": [row]})

    report = module.build_report([source])

    assert report["counts"]["resolved"] == 1
    assert report["tasks"][0]["direct_execution_proven"] is True


def test_weak_persisted_summary_is_technical_even_when_marked_not_executed(tmp_path):
    module = _load_module()
    row = _row(1, "task", "/run/task.log", 10, "eval_done", True)
    row["eval"]["executed"] = False
    del row["eval"]["summary"]["tests_status"]
    source = _write_json(tmp_path / "weak.json", {"rows": [row]})

    report = module.build_report([source])

    assert report["counts"]["resolved"] == 0
    assert report["counts"]["technical_failed_final"] == 1
    assert "missing_direct_execution_proof" in report["tasks"][0]["technical_reasons"]


def test_direct_proof_rejects_base_post_and_summary_evidence_tampering(tmp_path):
    module = _load_module()
    cases = {
        "base": lambda status: status.update(base_commit_status=1),
        "post": lambda status: status.update(post_before_base_status=1),
        "summary": lambda status: status.update(fail_to_pass_status=1),
        "evidence": lambda status: status["fail_to_pass_evidence"][0].update(
            status=1
        ),
    }
    for name, mutate in cases.items():
        row = _row(1, "task", f"/run/{name}.log", 10, "eval_done", True)
        mutate(row["eval"]["summary"]["tests_status"])
        source = _write_json(tmp_path / f"{name}.json", {"rows": [row]})

        report = module.build_report([source])

        assert report["counts"]["resolved"] == 0, name
        assert report["counts"]["technical_failed_final"] == 1, name
        assert (
            "missing_direct_execution_proof"
            in report["tasks"][0]["technical_reasons"]
        ), name


def test_direct_proof_requires_a_full_eval_spec_identity(tmp_path):
    module = _load_module()
    row = _row(1, "task", "/run/eval-spec.log", 10, "eval_done", True)
    row["eval"]["summary"]["eval_spec_sha256"] = "short"
    source = _write_json(tmp_path / "eval-spec.json", {"rows": [row]})

    report = module.build_report([source])

    assert report["counts"]["resolved"] == 0
    assert report["counts"]["technical_failed_final"] == 1
    assert "missing_direct_execution_proof" in report["tasks"][0]["technical_reasons"]


def test_empty_or_blocked_eval_status_is_a_technical_failure(tmp_path):
    module = _load_module()
    for name, status, reason in (
        ("empty", "", "missing_eval_status"),
        (
            "blocked",
            "blocked_missing_eval_image",
            "unexpected_eval_status:blocked_missing_eval_image",
        ),
    ):
        row = _row(1, "task", f"/run/{name}.log", 10, "eval_done", True)
        row["eval"]["status"] = status
        row["eval"]["executed"] = False
        source = _write_json(tmp_path / f"{name}.json", {"rows": [row]})

        report = module.build_report([source])

        assert report["counts"]["technical_failed_final"] == 1, name
        assert report["tasks"][0]["resolved"] is None, name
        assert reason in report["tasks"][0]["technical_reasons"], name


def test_minimal_dry_run_is_pending_but_failed_generation_is_technical(tmp_path):
    module = _load_module()
    dry = {
        "index": 1,
        "task": "task",
        "generation": {"status": "would_generate", "task": "task"},
        "eval": {"status": "would_eval"},
    }
    dry_source = _write_json(tmp_path / "dry.json", {"rows": [dry]})

    dry_report = module.build_report([dry_source])

    assert dry_report["counts"]["eval_pending"] == 1
    assert dry_report["counts"]["technical_failed_final"] == 0

    failed = _row(1, "task", "/run/failed.log", 10, "would_eval")
    failed["generation"]["status"] = "generation_failed"
    failed_source = _write_json(tmp_path / "failed.json", {"rows": [failed]})

    failed_report = module.build_report([failed_source])

    assert failed_report["counts"]["eval_pending"] == 0
    assert failed_report["counts"]["technical_failed_final"] == 1
    assert (
        "unexpected_generation_status:generation_failed"
        in failed_report["tasks"][0]["technical_reasons"]
    )


def test_empty_patch_rejects_summary_execution_and_attempt_conflicts(tmp_path):
    module = _load_module()
    cases = (
        (
            "resolved-summary",
            lambda row: row["eval"].update(summary={"resolved": True}),
            "empty_patch_eval_summary_not_empty",
        ),
        (
            "executed",
            lambda row: row["eval"].update(executed=True),
            "empty_patch_eval_execution_conflict",
        ),
        (
            "attempted",
            lambda row: row["eval"].update(attempt_count=1),
            "empty_patch_eval_attempt_count_invalid",
        ),
    )
    for name, mutate, reason in cases:
        row = _as_verified_empty(
            _row(1, "task", f"/run/{name}.log", 0, "skipped_empty_patch")
        )
        mutate(row)
        source = _write_json(tmp_path / f"{name}.json", {"rows": [row]})

        report = module.build_report([source])

        assert report["counts"]["technical_failed_final"] == 1, name
        assert report["tasks"][0]["resolved"] is None, name
        assert reason in report["tasks"][0]["technical_reasons"], name


def test_unreadable_expected_report_still_produces_expected_technical_rows(tmp_path):
    module = _load_module()
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"rows": []}), encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(real)

    report = module.build_report([link], expected_indices=(1, 2))

    assert report["counts"]["tasks"] == 2
    assert report["counts"]["technical_failed_final"] == 2
    assert all(
        any("unsafe_or_unstable_report_file" in reason for reason in task["technical_reasons"])
        for task in report["tasks"]
    )
