from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


def _load_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_eval_layer_report.py"
    spec = importlib.util.spec_from_file_location("swe_eval_layer_report", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _row(index: int, task: str, log: str, tokens: int, eval_status: str, resolved=None) -> dict:
    return {
        "index": index,
        "task": task,
        "generation": {
            "status": "generation_done",
            "log": log,
            "tokens_used": tokens,
            "steps": 3,
            "duration_s": 20,
            "record_id": f"record-{task}",
            "patch_sha256": f"sha-{task}",
        },
        "eval": {
            "status": eval_status,
            "attempt_count": 1,
            "summary": {
                "resolved": resolved,
                "patch_sha256": f"sha-{task}",
                "technical_reasons": ["fail_to_pass_infra"] if eval_status == "technical_eval_failed" else [],
                "report_path": f"/reports/{task}.json",
            },
        },
    }


def test_eval_layer_report_merges_two_rounds_and_deduplicates_generation_cost(tmp_path):
    module = _load_module()
    round1 = _write_json(
        tmp_path / "round1.json",
        {
            "rows": [
                _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed"),
                _row(2, "task-b", "/run/task-b.outer.log", 200, "eval_done", True),
            ]
        },
    )
    round2 = _write_json(
        tmp_path / "round2.json",
        {"rows": [_row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", False)]},
    )
    ignored_round3 = _write_json(
        tmp_path / "round3.json",
        {"rows": [_row(1, "task-a", "/run/task-a.outer.log", 100, "would_eval")]},
    )
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "workflow": {
                "records": [
                    {"path": "/run/task-a.outer.log", "tokens": 100, "steps": 3, "duration_s": 20},
                    {"path": "/run/task-b.outer.log", "tokens": 200, "steps": 4, "duration_s": 30},
                ]
            },
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/api/a.jsonl",
                        "pid": 11,
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.1,
                        "cost_usd_complete": True,
                    },
                    {
                        "api_usage_path": "/api/b.jsonl",
                        "pid": 22,
                        "calls": 4,
                        "total_tokens": 200,
                        "cost_usd": 0.2,
                        "cost_usd_complete": True,
                    },
                ]
            },
        },
    )

    report = module.build_report(
        [round1, round2, ignored_round3],
        token_cost_path=token_cost,
        max_rounds=2,
        usd_cny=7.0,
    )

    assert report["counts"]["tasks"] == 2
    assert report["counts"]["attempts"] == 3
    assert report["counts"]["eval_success"] == 2
    assert report["counts"]["resolved"] == 1
    assert report["counts"]["unresolved"] == 1
    assert report["counts"]["technical_failed_final"] == 0
    by_task = {task["task"]: task for task in report["tasks"]}
    assert by_task["task-a"]["eval_status"] == "eval_done"
    assert by_task["task-a"]["resolved"] is False
    assert by_task["task-a"]["attempt_count"] == 2
    assert by_task["task-a"]["token_cost"]["workflow_tokens"] == 100
    assert by_task["task-a"]["token_cost"]["cost_usd"] == 0.1
    assert by_task["task-a"]["token_cost"]["cost_cny"] == 0.7
    assert by_task["task-a"]["attempt_count"] == 2


def test_eval_layer_report_counts_empty_patch_without_technical_failure(tmp_path):
    module = _load_module()
    row = {
        "index": 17,
        "task": "instance_task-empty",
        "generation": {
            "status": "empty_patch",
            "workflow_status": "empty_patch_after_done",
            "patch_len": 0,
        },
        "eval": {
            "status": "skipped_empty_patch",
            "attempt_count": 0,
        },
    }
    source = _write_json(tmp_path / "empty.json", {"rows": [row]})
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/run/task_17/instance_task-empty/api_usage.jsonl",
                        "pid": 17,
                        "calls": 1,
                        "total_tokens": 322275,
                        "cost_usd": 0.18726324,
                        "cost_usd_complete": True,
                    }
                ]
            }
        },
    )

    report = module.build_report([source], token_cost_path=token_cost)

    assert report["counts"]["tasks"] == 1
    assert report["counts"]["empty_patch"] == 1
    assert report["counts"]["eval_attempts"] == 0
    assert report["counts"]["technical_failed_final"] == 0
    task = report["tasks"][0]
    assert task["token_cost"]["workflow_tokens"] == 322275
    assert task["token_cost"]["cost_usd"] == 0.18726324
    assert task["token_cost"]["api_usage_groups"][0]["match_reason"] == "matched_by_task_directory"


def test_eval_layer_report_rejects_mismatched_patch_sha_for_the_same_task(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed")
    second = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", False)
    second["generation"]["record_id"] = "record-task-a-repair"
    second["generation"]["patch_sha256"] = "sha-task-a-repair"
    second["eval"]["summary"]["patch_sha256"] = "sha-task-a-repair"
    round1 = _write_json(tmp_path / "round1.json", {"rows": [first]})
    round2 = _write_json(tmp_path / "round2.json", {"rows": [second]})
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "workflow": {
                "records": [
                    {"path": "/run/task-a.outer.log", "tokens": 100, "steps": 3, "duration_s": 20}
                ]
            },
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/api/a.jsonl",
                        "pid": 11,
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.1,
                        "cost_usd_complete": True,
                    }
                ]
            },
        },
    )

    try:
        module.build_report([round1, round2], token_cost_path=token_cost, max_rounds=2)
    except ValueError as exc:
        assert "candidate identity mismatch" in str(exc)
    else:
        raise AssertionError("mismatched patch SHA must not be merged")


def test_eval_layer_report_rejects_a_row_with_conflicting_candidate_identity(tmp_path):
    module = _load_module()
    row = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", True)
    row["eval"]["summary"]["patch_sha256"] = "sha-task-a-eval"
    report_path = _write_json(tmp_path / "round.json", {"rows": [row]})

    try:
        module.build_report([report_path])
    except ValueError as exc:
        assert "candidate identity mismatch within one task record" in str(exc)
    else:
        raise AssertionError("conflicting generation and evaluation SHA must fail")


def test_eval_layer_report_rejects_cross_run_third_eval_attempt(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed")
    first["eval"]["attempt_count"] = 2
    second = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", False)
    first_path = _write_json(tmp_path / "first.json", {"rows": [first]})
    second_path = _write_json(tmp_path / "second.json", {"rows": [second]})

    try:
        module.build_report([first_path, second_path], max_rounds=2)
    except ValueError as exc:
        assert "eval attempt budget exceeded" in str(exc)
        assert "task-a=3" in str(exc)
    else:
        raise AssertionError("a third cross-run evaluation must fail")


def test_eval_layer_report_preserves_over_budget_evidence_outside_final_result(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed")
    first["eval"]["attempt_count"] = 2
    second = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", False)
    first_path = _write_json(tmp_path / "first.json", {"rows": [first]})
    second_path = _write_json(tmp_path / "second.json", {"rows": [second]})

    report = module.build_report(
        [first_path, second_path],
        max_rounds=2,
        allow_over_budget_evidence=True,
    )

    task = report["tasks"][0]
    assert report["counts"]["eval_attempts"] == 2
    assert report["counts"]["observed_eval_attempts"] == 3
    assert report["counts"]["over_budget_tasks"] == 1
    assert report["counts"]["over_budget_eval_attempts"] == 1
    assert task["eval_status"] == "over_budget_evidence"
    assert task["resolved"] is None
    assert task["accepted_eval_status"] == "technical_eval_failed"
    assert task["over_budget_evidence"][0]["eval_status"] == "eval_done"
    assert task["over_budget_evidence"][0]["resolved"] is False


def test_eval_layer_report_rejects_missing_candidate_identity_across_reports(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed")
    first["generation"].pop("patch_sha256")
    first["eval"]["summary"].pop("patch_sha256")
    second = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", False)
    first_path = _write_json(tmp_path / "first.json", {"rows": [first]})
    second_path = _write_json(tmp_path / "second.json", {"rows": [second]})

    try:
        module.build_report([first_path, second_path], max_rounds=2)
    except ValueError as exc:
        assert "candidate identity unverified across reports" in str(exc)
    else:
        raise AssertionError("a missing SHA must block a cross-report merge")


def test_eval_layer_report_allows_verified_empty_patch_before_rerun(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 0, "skipped_empty_patch")
    first["generation"]["status"] = "empty_patch"
    first["generation"]["patch_len"] = 0
    first["generation"].pop("patch_sha256")
    first["eval"]["summary"].pop("patch_sha256")
    second = _row(1, "task-a", "/run/task-a-rerun.outer.log", 100, "eval_done", True)
    first_path = _write_json(tmp_path / "first.json", {"rows": [first]})
    second_path = _write_json(tmp_path / "second.json", {"rows": [second]})

    report = module.build_report([first_path, second_path], max_rounds=2)

    assert report["counts"]["tasks"] == 1
    assert report["counts"]["resolved"] == 1
    assert report["tasks"][0]["patch_sha256"] == "sha-task-a"


def test_eval_layer_report_rejects_incomplete_empty_patch_declarations(tmp_path):
    module = _load_module()
    mutations = (
        ("generation_status", lambda row: row["generation"].update(status="generation_done")),
        ("patch_len", lambda row: row["generation"].update(patch_len=1)),
        ("eval_status", lambda row: row["eval"].update(status="technical_eval_failed")),
    )
    for name, mutate in mutations:
        first = _row(1, "task-a", "/run/task-a.outer.log", 0, "skipped_empty_patch")
        first["generation"].update(status="empty_patch", patch_len=0)
        first["generation"].pop("patch_sha256")
        first["eval"]["summary"].pop("patch_sha256")
        mutate(first)
        second = _row(1, "task-a", "/run/task-a-rerun.outer.log", 100, "eval_done", True)
        first_path = _write_json(tmp_path / f"first-{name}.json", {"rows": [first]})
        second_path = _write_json(tmp_path / f"second-{name}.json", {"rows": [second]})

        try:
            module.build_report([first_path, second_path], max_rounds=2)
        except ValueError as exc:
            assert "candidate identity unverified across reports" in str(exc)
        else:
            raise AssertionError(f"incomplete empty patch declaration must fail: {name}")


def test_eval_layer_report_rejects_empty_patch_declaration_with_conflicting_sha(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 0, "skipped_empty_patch")
    first["generation"].update(status="empty_patch", patch_len=0)
    first["generation"]["patch_sha256"] = "sha-empty-claim"
    first["eval"]["summary"]["patch_sha256"] = "sha-empty-claim"
    second = _row(1, "task-a", "/run/task-a-rerun.outer.log", 100, "eval_done", True)
    first_path = _write_json(tmp_path / "first.json", {"rows": [first]})
    second_path = _write_json(tmp_path / "second.json", {"rows": [second]})

    try:
        module.build_report([first_path, second_path], max_rounds=2)
    except ValueError as exc:
        assert "candidate identity mismatch" in str(exc)
    else:
        raise AssertionError("an empty patch declaration with a SHA must retain identity checks")


def test_eval_layer_report_does_not_count_dry_run_as_an_official_eval(tmp_path):
    module = _load_module()
    row = _row(1, "task-a", "/run/task-a.outer.log", 100, "would_eval")
    row["eval"]["attempt_count"] = 1
    report_path = _write_json(tmp_path / "round.json", {"rows": [row]})

    report = module.build_report([report_path])

    assert report["counts"]["eval_attempts"] == 0
    assert report["tasks"][0]["eval_attempt_count"] == 0


def test_eval_layer_report_skips_eval_only_generation_token_fallback_duplicate(tmp_path):
    module = _load_module()
    first = _row(1, "task-a", "/run/task-a.outer.log", 100, "technical_eval_failed")
    second = _row(1, "task-a", None, 100, "eval_done", False)
    second["generation"]["steps"] = 99
    second["generation"]["duration_s"] = 999
    round1 = _write_json(tmp_path / "round1.json", {"rows": [first]})
    round2 = _write_json(tmp_path / "round2.json", {"rows": [second]})
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "workflow": {
                "records": [
                    {"path": "/run/task-a.outer.log", "tokens": 100, "steps": 3, "duration_s": 20}
                ]
            },
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/api/a.jsonl",
                        "pid": 11,
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.1,
                        "cost_usd_complete": True,
                    }
                ]
            },
        },
    )

    report = module.build_report([round1, round2], token_cost_path=token_cost, max_rounds=2)

    task = report["tasks"][0]
    assert task["attempt_count"] == 2
    assert task["eval_status"] == "eval_done"
    assert task["token_cost"]["workflow_tokens"] == 100
    assert task["token_cost"]["workflow_attempts"] == 1
    assert task["token_cost"]["cost_usd"] == 0.1
    assert task["token_cost"]["cost_usd_complete"] is True


def test_eval_layer_report_counts_nested_eval_retry_attempts(tmp_path):
    module = _load_module()
    row = _row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", True)
    row["eval"]["attempt_count"] = 2
    row["eval"]["attempts"] = [
        {"status": "technical_eval_failed", "summary": {"technical_reasons": ["redis"]}},
        {"status": "eval_done", "summary": {"resolved": True}},
    ]
    report_path = _write_json(tmp_path / "round.json", {"rows": [row]})

    report = module.build_report([report_path], max_rounds=2)

    assert report["counts"]["eval_attempts"] == 2
    assert report["counts"]["eval_retry_tasks"] == 1
    assert report["tasks"][0]["eval_attempt_count"] == 2


def test_eval_layer_report_refuses_ambiguous_token_cost_assignment(tmp_path):
    module = _load_module()
    report_path = _write_json(
        tmp_path / "round.json",
        {"rows": [_row(1, "task-a", "/logs/task-a.outer.log", 100, "eval_done", True)]},
    )
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "workflow": {
                "records": [
                    {"path": "/logs/task-a.outer.log", "tokens": 100, "steps": 3, "duration_s": 20}
                ]
            },
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/api/a.jsonl",
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.1,
                        "cost_usd_complete": True,
                    },
                    {
                        "api_usage_path": "/api/b.jsonl",
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.2,
                        "cost_usd_complete": True,
                    },
                ]
            },
        },
    )

    report = module.build_report([report_path], token_cost_path=token_cost)

    cost = report["tasks"][0]["token_cost"]
    assert cost["cost_usd"] is None
    assert cost["cost_usd_complete"] is False
    assert cost["cost_assignment_notes"][0]["reason"] == "ambiguous_api_usage_group"


def test_eval_layer_report_uses_path_and_call_count_to_assign_duplicate_token_costs(tmp_path):
    module = _load_module()
    report_path = _write_json(
        tmp_path / "round.json",
        {
            "rows": [
                _row(1, "task-a", "/run/task_a/generation_logs/a.outer.log", 100, "eval_done", True),
                _row(2, "task-b", "/run/task_b/generation_logs/b.outer.log", 100, "eval_done", False),
            ]
        },
    )
    token_cost = _write_json(
        tmp_path / "token_cost.json",
        {
            "workflow": {
                "records": [
                    {"path": "/run/task_a/generation_logs/a.outer.log", "tokens": 100, "steps": 3, "duration_s": 20},
                    {"path": "/run/task_b/generation_logs/b.outer.log", "tokens": 100, "steps": 4, "duration_s": 20},
                ]
            },
            "api_usage": {
                "groups": [
                    {
                        "api_usage_path": "/run/task_b/_runtime/repo/.opencollab/logs/api_usage.jsonl",
                        "calls": 4,
                        "total_tokens": 100,
                        "cost_usd": 0.4,
                        "cost_usd_complete": True,
                    },
                    {
                        "api_usage_path": "/run/task_a/_runtime/repo/.opencollab/logs/api_usage.jsonl",
                        "calls": 3,
                        "total_tokens": 100,
                        "cost_usd": 0.3,
                        "cost_usd_complete": True,
                    },
                ]
            },
        },
    )

    report = module.build_report([report_path], token_cost_path=token_cost)

    by_task = {task["task"]: task for task in report["tasks"]}
    assert by_task["task-a"]["token_cost"]["cost_usd"] == 0.3
    assert by_task["task-b"]["token_cost"]["cost_usd"] == 0.4
    assert by_task["task-a"]["token_cost"]["api_usage_groups"][0]["match_reason"] == "matched_by_context"


def test_eval_layer_report_cli_writes_json_and_markdown(tmp_path):
    report_json = _write_json(
        tmp_path / "round.json",
        {"rows": [_row(1, "task-a", "/run/task-a.outer.log", 100, "eval_done", True)]},
    )
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_eval_layer_report.py"

    proc = subprocess.run(
        [
            "python3",
            str(script),
            "--report-json",
            str(report_json),
            "--json-output",
            str(out_json),
            "--markdown-output",
            str(out_md),
        ],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0
    assert json.loads(out_json.read_text(encoding="utf-8"))["counts"]["resolved"] == 1
    assert "eval_success" in out_md.read_text(encoding="utf-8")


def test_eval_layer_report_keeps_dry_run_out_of_eval_failures(tmp_path):
    row = _row(7, "task-dry", "/run/task-dry.outer.log", 0, "would_eval")
    row["generation"]["status"] = "would_generate"
    report_path = _write_json(tmp_path / "dry.json", {"rows": [row]})
    module = _load_module()

    report = module.build_report([report_path])

    assert report["counts"]["eval_success"] == 0
    assert report["counts"]["eval_pending"] == 1
    assert report["counts"]["eval_failed"] == 0
