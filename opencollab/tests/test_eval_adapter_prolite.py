from __future__ import annotations

from pathlib import Path

from opencollab.harness.eval_adapter import (
    PROLITE_IMAGE_PREFIX,
    classify_technical_failure,
    load_jsonl_dataset,
    patch_candidate_from_diff,
    select_repo_root,
    task_spec_from_row,
    workspace_spec_for_task,
)


def _row(repo: str, tag: str, instance_id: str) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "repo": repo,
        "problem_statement": "Fix the bug.",
        "base_commit": "abc123",
        "dockerhub_tag": tag,
        "fail_to_pass": '["tests/f2p.test"]',
        "pass_to_pass": ["tests/p2p.test"],
    }


def test_prolite_rows_normalize_common_repositories() -> None:
    samples = [
        _row("NodeBB/NodeBB", "nodebb-nodebb-82562", "instance_NodeBB__NodeBB-82562"),
        _row("gravitational/teleport", "teleport-7744", "instance_gravitational__teleport-7744"),
        _row("future-architect/vuls", "vuls-dc496", "instance_future-architect__vuls-dc496"),
        _row("navidrome/navidrome", "navidrome-1e96", "instance_navidrome__navidrome-1e96"),
        _row("flipt-io/flipt", "flipt-9678", "instance_flipt-io__flipt-9678"),
    ]

    tasks = [task_spec_from_row(sample) for sample in samples]

    assert [task.repo for task in tasks] == [
        "NodeBB/NodeBB",
        "gravitational/teleport",
        "future-architect/vuls",
        "navidrome/navidrome",
        "flipt-io/flipt",
    ]
    assert all(task.docker_image.startswith(PROLITE_IMAGE_PREFIX) for task in tasks)
    assert tasks[0].service_dependencies == ("redis",)
    assert all(task.service_dependencies == () for task in tasks[1:])
    assert tasks[0].fail_to_pass == ("tests/f2p.test",)
    assert tasks[0].pass_to_pass == ("tests/p2p.test",)


def test_workspace_spec_prefers_app_then_testbed() -> None:
    task = task_spec_from_row(_row("future-architect/vuls", "vuls-edb324", "vuls-task"))
    spec = workspace_spec_for_task(task)

    assert spec.repo_root_candidates == ("/app", "/testbed")
    assert select_repo_root(["/tmp", "/testbed"]) == "/testbed"
    assert select_repo_root(["/testbed", "/app"]) == "/app"
    assert select_repo_root(["/workspace"]) == ""


def test_patch_candidate_records_sha_and_empty_patch() -> None:
    task = task_spec_from_row(_row("flipt-io/flipt", "flipt-cf06", "flipt-task"))

    empty = patch_candidate_from_diff(task=task, solver_name="fake", diff="")
    non_empty = patch_candidate_from_diff(
        task=task,
        solver_name="fake",
        diff="diff --git a/a b/a\n+change\n",
        token_count=12,
        cost_usd=0.25,
    )

    assert empty.is_empty
    assert not non_empty.is_empty
    assert len(non_empty.patch_sha256) == 64
    assert non_empty.token_count == 12
    assert non_empty.cost_usd == 0.25


def test_technical_failure_classification_is_specific() -> None:
    assert classify_technical_failure(
        log_text="Error: connect ECONNREFUSED 127.0.0.1:6379"
    ) == ("redis_unavailable",)
    assert classify_technical_failure(log_text="redis configured but tests passed") == ()
    assert classify_technical_failure(
        log_text="stat /app: no such file or directory",
        returncode=1,
    ) == ("workspace_root_missing",)
    assert classify_technical_failure(log_text="deadline exceeded") == ("timeout",)
    assert classify_technical_failure(returncode=2) == ("process_failed",)


def test_load_jsonl_dataset(tmp_path: Path) -> None:
    path = tmp_path / "instances.jsonl"
    path.write_text('{"instance_id":"a"}\n\n{"instance_id":"b"}\n', encoding="utf-8")

    assert load_jsonl_dataset(path) == [{"instance_id": "a"}, {"instance_id": "b"}]
