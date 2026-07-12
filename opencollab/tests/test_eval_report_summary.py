from __future__ import annotations

from pathlib import Path

from opencollab.harness.eval_adapter import EvalResult, PatchCandidate, RunRecord, TaskSpec
from opencollab.harness.eval_report import build_eval_summary


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        instance_id=task_id,
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
    )


def _candidate(task_id: str, patch: str, *, tokens: int = 0, cost: float = 0.0) -> PatchCandidate:
    return PatchCandidate(
        task_id=task_id,
        solver_name="baseTeam",
        patch=patch,
        token_count=tokens,
        cost_usd=cost,
    )


def test_build_eval_summary_counts_final_task_states() -> None:
    records = [
        RunRecord(
            task=_task("resolved"),
            solver_name="baseTeam",
            run_dir=Path("run/resolved"),
            attempt=1,
            candidate=_candidate("resolved", "diff --git a/a b/a\n+ok\n", tokens=10, cost=0.1),
            eval_result=EvalResult(
                task_id="resolved",
                patch_sha256="sha",
                eval_done=True,
                resolved=True,
            ),
        ),
        RunRecord(
            task=_task("unresolved"),
            solver_name="baseTeam",
            run_dir=Path("run/unresolved"),
            attempt=1,
            candidate=_candidate("unresolved", "diff --git a/a b/a\n+bad\n", tokens=20, cost=0.2),
            eval_result=EvalResult(
                task_id="unresolved",
                patch_sha256="sha",
                eval_done=True,
                resolved=False,
            ),
        ),
        RunRecord(
            task=_task("empty"),
            solver_name="baseTeam",
            run_dir=Path("run/empty"),
            attempt=1,
            candidate=_candidate("empty", "", tokens=5, cost=0.05),
            eval_result=None,
        ),
        RunRecord(
            task=_task("technical"),
            solver_name="baseTeam",
            run_dir=Path("run/technical/1"),
            attempt=1,
            candidate=_candidate("technical", "diff --git a/a b/a\n+try\n", tokens=7, cost=0.07),
            eval_result=EvalResult(
                task_id="technical",
                patch_sha256="sha",
                eval_done=False,
                resolved=False,
                technical_failed=True,
                technical_reasons=("redis_unavailable",),
            ),
        ),
        RunRecord(
            task=_task("technical"),
            solver_name="baseTeam",
            run_dir=Path("run/technical/2"),
            attempt=2,
            candidate=_candidate("technical", "diff --git a/a b/a\n+try2\n", tokens=8, cost=0.08),
            eval_result=EvalResult(
                task_id="technical",
                patch_sha256="sha2",
                eval_done=True,
                resolved=True,
            ),
        ),
    ]

    summary = build_eval_summary(records, run_id="run1", solver="baseTeam", usd_cny=7.0)

    assert summary["counts"] == {
        "tasks": 4,
        "generation_done": 4,
        "empty_patch": 1,
        "official_eval_done": 3,
        "resolved": 2,
        "unresolved": 1,
        "technical_failed": 0,
        "missing_eval": 0,
        "retry_tasks": 1,
    }
    assert summary["token_cost"]["total_tokens"] == 50
    assert summary["token_cost"]["cost_usd"] == 0.5
    assert summary["token_cost"]["cost_cny"] == 3.5
    by_task = {row["task_id"]: row for row in summary["rows"]}
    assert by_task["technical"]["attempts"] == 2
    assert by_task["technical"]["final_classification"] == "resolved"
    assert by_task["empty"]["final_classification"] == "empty_patch"
