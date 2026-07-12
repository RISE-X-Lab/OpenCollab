from __future__ import annotations

import hashlib
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


def _eval_result(
    candidate: PatchCandidate,
    *,
    eval_done: bool,
    resolved: bool,
    technical_failed: bool = False,
    technical_reasons: tuple[str, ...] = (),
) -> EvalResult:
    return EvalResult(
        task_id=candidate.task_id,
        patch_sha256=candidate.patch_sha256,
        eval_done=eval_done,
        resolved=resolved,
        technical_failed=technical_failed,
        technical_reasons=technical_reasons,
    )


def test_build_eval_summary_counts_final_task_states() -> None:
    resolved = _candidate("resolved", "diff --git a/a b/a\n+ok\n", tokens=10, cost=0.1)
    unresolved = _candidate("unresolved", "diff --git a/a b/a\n+bad\n", tokens=20, cost=0.2)
    technical_first = _candidate("technical", "diff --git a/a b/a\n+try\n", tokens=7, cost=0.07)
    technical_final = _candidate("technical", "diff --git a/a b/a\n+try2\n", tokens=8, cost=0.08)
    records = [
        RunRecord(
            task=_task("resolved"),
            solver_name="baseTeam",
            run_dir=Path("run/resolved"),
            attempt=1,
            candidate=resolved,
            eval_result=_eval_result(resolved, eval_done=True, resolved=True),
        ),
        RunRecord(
            task=_task("unresolved"),
            solver_name="baseTeam",
            run_dir=Path("run/unresolved"),
            attempt=1,
            candidate=unresolved,
            eval_result=_eval_result(unresolved, eval_done=True, resolved=False),
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
            candidate=technical_first,
            eval_result=_eval_result(
                technical_first,
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
            candidate=technical_final,
            eval_result=_eval_result(technical_final, eval_done=True, resolved=True),
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


def test_build_eval_summary_rejects_unpaired_or_contradictory_eval_evidence() -> None:
    candidate = _candidate("task", "diff --git a/a b/a\n+fix\n")
    cases = {
        "wrong_task": EvalResult(
            task_id="other",
            patch_sha256=candidate.patch_sha256,
            eval_done=True,
            resolved=True,
        ),
        "old_patch": EvalResult(
            task_id="task",
            patch_sha256="a" * 64,
            eval_done=True,
            resolved=True,
        ),
        "short_hash": EvalResult(
            task_id="task",
            patch_sha256=candidate.patch_sha256[:12],
            eval_done=True,
            resolved=True,
        ),
        "technical_resolved_conflict": EvalResult(
            task_id="task",
            patch_sha256=candidate.patch_sha256,
            eval_done=True,
            resolved=True,
            technical_failed=True,
        ),
    }

    for name, eval_result in cases.items():
        summary = build_eval_summary(
            [
                RunRecord(
                    task=_task("task"),
                    solver_name="baseTeam",
                    run_dir=Path(f"run/{name}"),
                    attempt=1,
                    candidate=candidate,
                    eval_result=eval_result,
                )
            ],
            run_id=name,
            solver="baseTeam",
        )

        assert summary["counts"]["technical_failed"] == 1, name
        assert summary["counts"]["resolved"] == 0, name
        assert summary["counts"]["official_eval_done"] == 0, name
        row = summary["rows"][0]
        assert row["final_classification"] == "technical_failed", name
        assert row["eval_done"] is False, name
        assert row["resolved"] is None, name
        assert row["technical_reasons"], name


def test_build_eval_summary_marks_conflicting_verdicts_for_one_patch_technical() -> None:
    candidate = _candidate("task", "diff --git a/a b/a\n+fix\n")
    records = [
        RunRecord(
            task=_task("task"),
            solver_name="baseTeam",
            run_dir=Path("run/first"),
            attempt=1,
            candidate=candidate,
            eval_result=_eval_result(candidate, eval_done=True, resolved=True),
        ),
        RunRecord(
            task=_task("task"),
            solver_name="baseTeam",
            run_dir=Path("run/second"),
            attempt=2,
            candidate=candidate,
            eval_result=_eval_result(candidate, eval_done=True, resolved=False),
        ),
    ]

    summary = build_eval_summary(records, run_id="conflict", solver="baseTeam")

    assert summary["counts"]["technical_failed"] == 1
    assert summary["counts"]["official_eval_done"] == 0
    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["unresolved"] == 0
    assert summary["rows"][0]["resolved"] is None
    assert summary["rows"][0]["eval_done"] is False
    assert "conflicting_eval_verdicts" in summary["rows"][0]["technical_reasons"]


def test_build_eval_summary_rejects_empty_candidate_task_mismatch() -> None:
    candidate = _candidate("wrong-task", "")
    record = RunRecord(
        task=_task("task"),
        solver_name="baseTeam",
        run_dir=Path("run/empty-mismatch"),
        attempt=1,
        candidate=candidate,
    )

    summary = build_eval_summary([record], run_id="empty-mismatch", solver="baseTeam")

    assert summary["counts"]["empty_patch"] == 1
    assert summary["counts"]["technical_failed"] == 1
    assert summary["rows"][0]["final_classification"] == "technical_failed"
    assert "candidate_task_mismatch" in summary["rows"][0]["technical_reasons"]


def test_build_eval_summary_rejects_resolved_eval_for_empty_candidate() -> None:
    candidate = _candidate("task", "")
    record = RunRecord(
        task=_task("task"),
        solver_name="baseTeam",
        run_dir=Path("run/empty-resolved"),
        attempt=1,
        candidate=candidate,
        eval_result=EvalResult(
            task_id="task",
            patch_sha256=hashlib.sha256(b"").hexdigest(),
            eval_done=True,
            resolved=True,
        ),
    )

    summary = build_eval_summary([record], run_id="empty-resolved", solver="baseTeam")

    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["technical_failed"] == 1
    assert summary["rows"][0]["resolved"] is None
    assert "empty_candidate_eval_conflict" in summary["rows"][0]["technical_reasons"]
