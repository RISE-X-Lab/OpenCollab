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
from opencollab.adapters import _atomic_rename as atomic_rename_mod
from opencollab.adapters import _owned_file_cleanup as owned_cleanup_mod
from opencollab.adapters import safe_files as safe_files_mod


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
    assert not temp_directory.exists()
    assert list(tmp_path.glob(".oc-*.tmp")) == []


def test_save_results_rejects_oversized_total_before_replace(monkeypatch, tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text('{"old": true}\n', encoding="utf-8")
    result = EvalResult(
        task_id="total-limit",
        patch="x",
        patch_produced=True,
        tokens_used=0,
        steps=0,
        duration=0.0,
    )
    monkeypatch.setattr(evaluator, "MAX_RESULTS_FILE_BYTES", 1)

    with pytest.raises(ValueError, match="evaluation results exceed"):
        save_results([result], str(output))

    assert output.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(tmp_path.glob(".oc-*.tmp")) == []


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


def test_save_results_detects_parent_swap_after_atomic_commit(
    monkeypatch,
    tmp_path,
):
    parent = tmp_path / "output"
    parent.mkdir()
    moved_parent = tmp_path / "moved-output"
    output = parent / "results.jsonl"
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def commit_then_swap_parent(source, target, **kwargs):
        result = real_rename_noreplace(source, target, **kwargs)
        if target == output.name:
            parent.rename(moved_parent)
            parent.mkdir()
        return result

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        commit_then_swap_parent,
    )

    with pytest.raises(OSError, match="parent changed after atomic create"):
        save_results([], str(output))

    assert output.exists() is False
    assert not (moved_parent / "results.jsonl").exists()
    assert len(list(moved_parent.glob(".opencollab-retired-*"))) == 1


def test_save_results_detects_target_swap_after_atomic_commit(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def commit_then_swap_target(source, target, **kwargs):
        result = real_rename_noreplace(source, target, **kwargs)
        parent_fd = kwargs["dst_dir_fd"]
        os.rename(
            target,
            f"{target}.detached",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        replacement_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        os.close(replacement_fd)
        return result

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        commit_then_swap_target,
    )

    with pytest.raises(OSError, match="changed during create"):
        save_results([], str(output))

    assert output.is_file()
    assert output.read_bytes() == b""
    assert (tmp_path / "results.jsonl.detached").is_file()


def test_save_results_rejects_swapped_temp_without_touching_victim(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    victim = tmp_path / "victim.jsonl"
    victim.write_text("foreign\n", encoding="utf-8")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def swap_temp_before_commit(source, target, **kwargs):
        parent_fd = kwargs["src_dir_fd"]
        os.rename(
            source,
            f"{source}.detached",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.symlink(victim, source, dir_fd=parent_fd)
        return real_rename_noreplace(source, target, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        swap_temp_before_commit,
    )

    with pytest.raises(OSError, match="changed during create"):
        save_results([], str(output))

    assert output.is_symlink()
    assert output.resolve() == victim
    assert victim.read_text(encoding="utf-8") == "foreign\n"


def test_save_results_rejects_concurrent_successor_before_commit(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    output.write_text("old\n", encoding="utf-8")
    original_write = evaluator.write_regular_file_atomic

    def replace_before_write(path, writer, **kwargs):
        output.unlink()
        output.write_text("successor\n", encoding="utf-8")
        return original_write(path, writer, **kwargs)

    monkeypatch.setattr(evaluator, "write_regular_file_atomic", replace_before_write)

    with pytest.raises(OSError, match="target identity changed before commit"):
        save_results([], str(output))

    assert output.read_text(encoding="utf-8") == "successor\n"
    assert list(tmp_path.glob(".oc-*.tmp")) == []


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

    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def fail_target_commit(source, target, **kwargs):
        if target == out.name:
            raise OSError("simulated replace failure")
        return real_rename_noreplace(source, target, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        fail_target_commit,
    )

    with pytest.raises(OSError, match="simulated replace failure"):
        save_results([result], str(out))

    assert out.read_text(encoding="utf-8") == '{"old": true}\n'
    temp_directory = out.parent / evaluator.RESULT_TEMP_DIRECTORY
    assert not temp_directory.exists()
    assert list(out.parent.glob(".oc-*.tmp")) == []


def test_save_results_preserves_primary_error_when_temp_cleanup_fails(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    output.write_text("old\n", encoding="utf-8")

    def fail_file_fsync(_fd):
        raise OSError("primary results fsync failure")

    def fail_temp_retirement(*args, **kwargs):
        raise OSError("secondary results cleanup failure")

    monkeypatch.setattr(safe_files_mod.os, "fsync", fail_file_fsync)
    monkeypatch.setattr(
        owned_cleanup_mod,
        "finalize_owned_retirement_candidate",
        fail_temp_retirement,
    )

    with pytest.raises(OSError, match="primary results fsync failure") as captured:
        save_results([], str(output))

    assert output.read_text(encoding="utf-8") == "old\n"
    assert any(
        "secondary results cleanup failure" in note
        for note in getattr(captured.value, "__notes__", [])
    )


def test_save_results_reports_directory_fsync_failure_after_replace(
    monkeypatch,
    tmp_path,
):
    out = tmp_path / "results.jsonl"
    real_fsync = evaluator.os.fsync
    fsync_calls = 0

    def fail_directory_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("results directory fsync failed")
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

    with pytest.raises(OSError, match="results directory fsync failed"):
        save_results([result], str(out))

    assert fsync_calls == 3
    assert not out.exists()
    temp_directory = tmp_path / evaluator.RESULT_TEMP_DIRECTORY
    assert not temp_directory.exists()
    assert list(tmp_path.glob(".oc-*.tmp")) == []
