from __future__ import annotations

from evaluator_test_support import (
    SUBMISSION_INTEGRITY_PROVEN,
    EvalResult,
    EvalTask,
    eval_cli,
    evaluator,
    json,
    metric_submission_integrity,
    os,
    pytest,
    run,
    run_eval_task,
    save_results,
)


def test_save_results_writes_patch_produced_key(tmp_path):
    results = [
        EvalResult(
            task_id="t1",
            patch="diff --git\n+x\n",
            patch_produced=True,
            tokens_used=5,
            steps=1,
            duration=0.123,
            checkpoint_result={
                "restore": {
                    "status": "restored",
                    "worktree_integrity_proven": True,
                    "submission_eligible": True,
                }
            },
        ),
    ]
    out = tmp_path / "results.jsonl"
    save_results(results, str(out))

    record = json.loads(out.read_text().strip())
    assert record["patch_produced"] is True
    assert "success" not in record
    assert record["task_id"] == "t1"
    assert record["patch"] == "diff --git\n+x\n"
    assert record["patch_lines"] == 2
    assert record["test_patch_isolation_failed"] is False
    assert record["execution_quiesced"] is True
    assert record["patch_extraction_succeeded"] is True
    assert record["injected_path_cleanup_proven"] is True
    assert record["harness_artifact_exclusion_proven"] is True
    assert record["checkpoint_restore_integrity_proven"] is True
    assert record["task_stage_integrity_proven"] is True
    assert record["submission_eligible"] is True
    assert record["checkpoint_result"]["restore"]["worktree_integrity_proven"] is True
    assert metric_submission_integrity(record) == SUBMISSION_INTEGRITY_PROVEN


def test_save_results_supports_name_max_destination(tmp_path):
    output = tmp_path / ("r" * 255)

    save_results([], str(output))

    assert output.read_bytes() == b""


def test_save_results_rejects_oversized_record_before_replace(monkeypatch, tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text('{"old": true}\n', encoding="utf-8")
    result = EvalResult(
        task_id="large",
        patch="x" * 200,
        patch_produced=True,
        tokens_used=0,
        steps=0,
        duration=0.0,
    )
    monkeypatch.setattr(evaluator, "MAX_RESULT_RECORD_BYTES", 64)

    with pytest.raises(ValueError, match="record exceeds"):
        save_results([result], str(output))

    assert output.read_text(encoding="utf-8") == '{"old": true}\n'
    temp_directory = tmp_path / evaluator.RESULT_TEMP_DIRECTORY
    assert list(temp_directory.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True], ids=["root", "intermediate"])
def test_run_eval_task_rejects_symlink_output_directory_without_external_writes(
    tmp_path,
    nested,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        safe_parent = tmp_path / "safe"
        safe_parent.mkdir()
        link = safe_parent / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output_dir = link / "nested"
    else:
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output_dir = link

    with pytest.raises(OSError, match="not a real directory"):
        run(
            run_eval_task(
                EvalTask(task_id="symlink-output", description="x"),
                output_dir=str(output_dir),
                tools_factory=list,
            )
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True], ids=["root", "intermediate"])
def test_save_results_rejects_symlink_parent_without_external_writes(
    tmp_path,
    nested,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        safe_parent = tmp_path / "safe"
        safe_parent.mkdir()
        link = safe_parent / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output = link / "nested" / "results.jsonl"
    else:
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output = link / "results.jsonl"

    with pytest.raises(OSError, match="not a real directory"):
        save_results([], str(output))

    assert list(outside.iterdir()) == []


def test_save_results_rejects_symlink_target_without_overwriting_destination(
    tmp_path,
):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    output = tmp_path / "results.jsonl"
    output.symlink_to(outside)

    with pytest.raises(OSError, match="not a regular file"):
        save_results([], str(output))

    assert outside.read_text(encoding="utf-8") == '{"outside": true}\n'


def test_save_results_detects_parent_swap_after_dirfd_replace(
    monkeypatch,
    tmp_path,
):
    parent = tmp_path / "output"
    parent.mkdir()
    moved_parent = tmp_path / "moved-output"
    output = parent / "results.jsonl"
    real_replace = evaluator.os.replace

    def replace_then_swap(source, target, **kwargs):
        real_replace(source, target, **kwargs)
        parent.rename(moved_parent)
        parent.mkdir()

    monkeypatch.setattr(evaluator.os, "replace", replace_then_swap)

    with pytest.raises(OSError, match="parent changed after atomic replace"):
        save_results([], str(output))

    assert output.exists() is False
    assert (moved_parent / "results.jsonl").is_file()


def test_save_results_detects_target_swap_after_dirfd_replace(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    real_replace = evaluator.os.replace

    def replace_then_swap_target(source, target, **kwargs):
        real_replace(source, target, **kwargs)
        parent_fd = kwargs["dst_dir_fd"]
        os.unlink(target, dir_fd=parent_fd)
        replacement_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        os.close(replacement_fd)

    monkeypatch.setattr(evaluator.os, "replace", replace_then_swap_target)

    with pytest.raises(OSError, match="target changed after atomic replace"):
        save_results([], str(output))


def test_ineligible_safe_patch_remains_observable_but_is_not_counted(tmp_path):
    result = EvalResult(
        task_id="cleanup-unproven",
        patch="diff --git a/src/app.py b/src/app.py\n+safe internal diff\n",
        patch_produced=True,
        tokens_used=5,
        steps=1,
        duration=0.123,
        injected_path_cleanup_proven=False,
        submission_eligible=False,
    )
    out = tmp_path / "results.jsonl"

    save_results([result], str(out))

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["patch_produced"] is True
    assert record["patch"].startswith("diff --git")
    assert record["injected_path_cleanup_proven"] is False
    assert record["submission_eligible"] is False
    assert eval_cli._result_counts([result]) == (0, 1)


def test_save_results_failure_preserves_previous_file(monkeypatch, tmp_path):
    out = tmp_path / "nested" / "results.jsonl"
    out.parent.mkdir()
    out.write_text('{"old": true}\n', encoding="utf-8")
    result = EvalResult(
        task_id="replacement",
        patch="diff --git a/x b/x\n+new\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=0.1,
    )

    def fail_replace(source, target, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(evaluator.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_results([result], str(out))

    assert out.read_text(encoding="utf-8") == '{"old": true}\n'
    temp_directory = out.parent / evaluator.RESULT_TEMP_DIRECTORY
    assert temp_directory.is_dir()
    assert list(temp_directory.iterdir()) == []


@pytest.mark.parametrize(
    "failing_call, failure_message",
    [
        (2, "source directory fsync failed"),
        (3, "destination directory fsync failed"),
    ],
    ids=["source-directory", "destination-directory"],
)
def test_save_results_reports_directory_fsync_failure_after_replace(
    monkeypatch,
    tmp_path,
    failing_call,
    failure_message,
):
    out = tmp_path / "results.jsonl"
    real_fsync = evaluator.os.fsync
    fsync_calls = 0

    def fail_directory_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == failing_call:
            raise OSError(failure_message)
        return real_fsync(fd)

    monkeypatch.setattr(evaluator.os, "fsync", fail_directory_fsync)
    result = EvalResult(
        task_id="durability",
        patch="",
        patch_produced=False,
        tokens_used=0,
        steps=0,
        duration=0.1,
    )

    with pytest.raises(OSError, match=failure_message):
        save_results([result], str(out))

    assert fsync_calls == 3
    assert json.loads(out.read_text(encoding="utf-8"))["task_id"] == "durability"
    temp_directory = tmp_path / evaluator.RESULT_TEMP_DIRECTORY
    assert temp_directory.is_dir()
    assert list(temp_directory.iterdir()) == []
