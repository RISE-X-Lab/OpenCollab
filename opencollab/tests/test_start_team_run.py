from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from start_team_run_test_support import _rows, _run, _seed_pending_prediction


def test_team_runner_preserves_nonzero_agent_status_after_clean_stop(fake_team_repo):
    result = _run(fake_team_repo, env={"FAKE_AGENT_RC": "7"})

    assert result.returncode == 7, result.stderr
    rows = _rows(fake_team_repo["output"])
    assert len(rows) == 1
    metric = rows[0]["workflow_metric"]
    assert metric["workflow_status"] == "error"
    assert metric["runner_returncode"] == 7
    assert metric["submission_eligible"] is True
    assert metric["execution_quiesced"] is True
    assert metric["patch_extraction_succeeded"] is True
    assert metric["injected_path_cleanup_proven"] is True
    assert metric["harness_artifact_exclusion_proven"] is True
    assert metric["checkpoint_restore_integrity_proven"] is True
    assert metric["task_stage_integrity_proven"] is True
    assert metric["test_patch_isolation_failed"] is False
    assert metric["worktree_integrity_proven"] is True
    assert metric["patch_produced"] is True
    assert rows[0]["model_patch"].startswith("diff --git")
    assert not (fake_team_repo["state"] / "pending_prediction.record.json").exists()
    assert not (fake_team_repo["state"] / "pending_prediction.patch").exists()


def test_team_runner_stop_failure_destroys_container_without_append(fake_team_repo):
    result = _run(
        fake_team_repo,
        env={"FAKE_AGENT_RC": "124", "FAKE_STOP_RC": "125"},
    )

    assert result.returncode != 0
    assert _rows(fake_team_repo["output"]) == []
    assert "container process group did not quiesce" in result.stderr
    assert not (fake_team_repo["state"] / "team_container.owner").exists()
    assert "rm -f" in fake_team_repo["log"].read_text(encoding="utf-8")


def test_team_runner_unknown_cleanup_keeps_owner_and_pending_without_append(fake_team_repo):
    result = _run(fake_team_repo, env={"FAKE_CLEANUP_MODE": "unknown"})

    assert result.returncode == 125
    assert _rows(fake_team_repo["output"]) == []
    assert (fake_team_repo["state"] / "team_container.owner").exists()
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()
    assert (fake_team_repo["state"] / "pending_prediction.patch").exists()


def test_team_runner_append_failure_preserves_authoritative_pending_files(fake_team_repo):
    output_directory = fake_team_repo["root"] / "output-is-directory"
    output_directory.mkdir()

    result = _run(fake_team_repo, output=output_directory)

    assert result.returncode != 0
    assert "patch preserved" in result.stderr
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()
    assert (fake_team_repo["state"] / "pending_prediction.patch").exists()
    assert not (fake_team_repo["state"] / "team_container.owner").exists()


def test_team_runner_replays_pending_once_and_exits_before_docker_run(fake_team_repo):
    state = fake_team_repo["state"]
    record = _seed_pending_prediction(fake_team_repo)

    result = _run(fake_team_repo)

    assert result.returncode == 0, result.stderr
    assert _rows(fake_team_repo["output"]) == [record]
    assert not fake_team_repo["log"].exists()
    assert not (state / "pending_prediction.record.json").exists()
    assert not (state / "pending_prediction.patch").exists()


@pytest.mark.parametrize(
    "missing_field",
    [
        "submission_eligible",
        "execution_quiesced",
        "patch_extraction_succeeded",
        "injected_path_cleanup_proven",
        "harness_artifact_exclusion_proven",
        "checkpoint_restore_integrity_proven",
        "task_stage_integrity_proven",
        "test_patch_isolation_failed",
        "worktree_integrity_proven",
        "patch_produced",
    ],
)
def test_team_runner_refuses_pending_record_with_missing_integrity_proof(
    fake_team_repo,
    missing_field,
):
    state = fake_team_repo["state"]
    _seed_pending_prediction(
        fake_team_repo,
        omit_integrity_field=missing_field,
    )

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert _rows(fake_team_repo["output"]) == []
    assert (state / "pending_prediction.record.json").exists()
    assert (state / "pending_prediction.patch").exists()


def test_team_runner_refuses_symlink_prediction_output_without_touching_target(
    fake_team_repo,
):
    target = fake_team_repo["root"] / "outside.jsonl"
    target.write_text("", encoding="utf-8")
    link = fake_team_repo["root"] / "linked-output.jsonl"
    link.symlink_to(target)
    _seed_pending_prediction(fake_team_repo, output=link)

    result = _run(fake_team_repo, output=link)

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == ""
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()


def test_team_runner_refuses_symlink_prediction_parent_without_outside_write(
    fake_team_repo,
):
    outside = fake_team_repo["root"] / "outside-output"
    outside.mkdir()
    linked_parent = fake_team_repo["root"] / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    output = linked_parent / "predictions.jsonl"
    _seed_pending_prediction(fake_team_repo, output=output)

    result = _run(fake_team_repo, output=output)

    assert result.returncode != 0
    assert list(outside.iterdir()) == []
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()
    assert (fake_team_repo["state"] / "pending_prediction.patch").exists()


@pytest.mark.parametrize("corrupt_row", ['{"instance_id":', "[]\n"])
def test_team_runner_rejects_corrupt_prediction_jsonl_and_preserves_pending(
    fake_team_repo,
    corrupt_row,
):
    output = fake_team_repo["output"]
    output.write_text(corrupt_row, encoding="utf-8")
    _seed_pending_prediction(fake_team_repo)

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == corrupt_row
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()
    assert (fake_team_repo["state"] / "pending_prediction.patch").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_team_runner_refuses_fifo_prediction_output_without_blocking(fake_team_repo):
    output_fifo = fake_team_repo["root"] / "predictions.fifo"
    os.mkfifo(output_fifo)
    _seed_pending_prediction(fake_team_repo, output=output_fifo)

    result = _run(fake_team_repo, output=output_fifo)

    assert result.returncode != 0
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()


def test_team_runner_prediction_output_lock_has_bounded_wait(fake_team_repo):
    output = fake_team_repo["output"]
    output.touch()
    _seed_pending_prediction(fake_team_repo)
    holder = os.open(output, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run(
            fake_team_repo,
            env={"OPENCOLLAB_HARNESS_LOCK_TIMEOUT_SECONDS": "0.05"},
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert result.returncode != 0
    assert "timed out acquiring prediction output lock" in result.stderr
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()


def test_team_runner_refuses_oversized_prediction_output(fake_team_repo):
    output = fake_team_repo["output"]
    with output.open("wb") as handle:
        handle.truncate(256 * 1024 * 1024 + 1)
    _seed_pending_prediction(fake_team_repo)

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert "prediction output exceeds byte limit" in result.stderr
    assert output.stat().st_size == 256 * 1024 * 1024 + 1
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()


def test_team_runner_refuses_pending_output_path_mismatch(fake_team_repo):
    redirected = fake_team_repo["root"] / "redirected.jsonl"
    _seed_pending_prediction(fake_team_repo, output=redirected)

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert not redirected.exists()
    assert (fake_team_repo["state"] / "pending_prediction.record.json").exists()


@pytest.mark.parametrize(
    "instance_id",
    ["../../outside", "/absolute", "nested/task", r"nested\\task", "task\nname"],
)
def test_team_runner_rejects_unsafe_instance_id_before_session_cleanup(
    fake_team_repo,
    instance_id,
):
    instance = json.loads(fake_team_repo["instance"].read_text(encoding="utf-8"))
    instance["instance_id"] = instance_id
    fake_team_repo["instance"].write_text(json.dumps(instance), encoding="utf-8")
    sentinel = (
        fake_team_repo["root"]
        / ".opencollab"
        / "outside"
        / "loop_monitor_artifacts"
        / "sentinel"
    )
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep", encoding="utf-8")

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_symlink_instance_file(fake_team_repo):
    target = fake_team_repo["root"] / "real-instance.json"
    fake_team_repo["instance"].replace(target)
    fake_team_repo["instance"].symlink_to(target)

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert "bounded validation" in result.stderr
    assert not fake_team_repo["log"].exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_team_runner_rejects_fifo_instance_without_blocking(fake_team_repo):
    fake_team_repo["instance"].unlink()
    os.mkfifo(fake_team_repo["instance"])

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_oversized_instance(fake_team_repo):
    with fake_team_repo["instance"].open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert "bounded validation" in result.stderr
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_instance_replacement_during_read(fake_team_repo):
    hook_dir = fake_team_repo["root"] / "read-race-hook"
    hook_dir.mkdir()
    replacement = fake_team_repo["root"] / "replacement-instance.json"
    replacement.write_text(
        json.dumps(
            {
                "instance_id": "demo__task-2",
                "repo": "demo/repo",
                "problem_statement": "replacement",
            }
        ),
        encoding="utf-8",
    )
    (hook_dir / "sitecustomize.py").write_text(
        """
import os

_original_read = os.read
_raced = False


def _race_read(fd, size):
    global _raced
    data = _original_read(fd, size)
    target = os.environ.get("OC_TEST_RACE_INSTANCE")
    replacement = os.environ.get("OC_TEST_RACE_REPLACEMENT")
    expected_inode = int(os.environ.get("OC_TEST_RACE_INODE", "0"))
    if not _raced and target and replacement and os.fstat(fd).st_ino == expected_inode:
        _raced = True
        os.replace(replacement, target)
    return data


os.read = _race_read
""",
        encoding="utf-8",
    )
    env = {
        "PYTHONPATH": str(hook_dir),
        "OC_TEST_RACE_INSTANCE": str(fake_team_repo["instance"]),
        "OC_TEST_RACE_REPLACEMENT": str(replacement),
        "OC_TEST_RACE_INODE": str(fake_team_repo["instance"].stat().st_ino),
    }

    result = _run(fake_team_repo, env=env)

    assert result.returncode != 0
    assert "changed while reading" in result.stderr
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_task_file_replacement_during_write(fake_team_repo):
    hook_dir = fake_team_repo["root"] / "write-race-hook"
    hook_dir.mkdir()
    victim = fake_team_repo["root"] / "moved-task-victim"
    (hook_dir / "sitecustomize.py").write_text(
        """
import os
import pathlib
import tempfile

_original_replace = os.replace
_original_stat = os.stat
_raced = False


def _race_stat(path, *args, **kwargs):
    global _raced
    name = os.fsdecode(path) if isinstance(path, (str, bytes)) else ""
    if (
        not _raced
        and name.startswith("oc_task.")
        and kwargs.get("dir_fd") is not None
    ):
        candidate = pathlib.Path(tempfile.gettempdir()) / name
        victim = pathlib.Path(os.environ["OC_TEST_TASK_VICTIM"])
        _original_replace(candidate, victim)
        candidate.write_bytes(b"attacker replacement")
        _raced = True
    return _original_stat(path, *args, **kwargs)


os.stat = _race_stat
""",
        encoding="utf-8",
    )

    result = _run(
        fake_team_repo,
        env={
            "PYTHONPATH": str(hook_dir),
            "OC_TEST_TASK_VICTIM": str(victim),
        },
    )

    assert result.returncode != 0
    assert "changed while writing" in result.stderr
    assert victim.read_bytes() == b""
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_symlink_session_root(fake_team_repo):
    real_session = fake_team_repo["root"] / "real-session"
    real_session.mkdir()
    linked_session = fake_team_repo["root"] / "linked-session"
    linked_session.symlink_to(real_session, target_is_directory=True)

    result = _run(fake_team_repo, session_root=linked_session)

    assert result.returncode != 0
    assert "session root failed safe directory validation" in result.stderr
    assert not fake_team_repo["log"].exists()


def test_team_runner_refuses_container_writable_legacy_owner_without_destroy(
    fake_team_repo,
):
    session = fake_team_repo["session"]
    session.mkdir(parents=True)
    (session / "team_container.owner").write_text(
        "oc-team-forged-victim\n",
        encoding="utf-8",
    )

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert "legacy container-writable harness state" in result.stderr
    if fake_team_repo["log"].exists():
        assert "oc-team-forged-victim" not in fake_team_repo["log"].read_text(
            encoding="utf-8"
        )


def test_team_runner_host_state_is_outside_container_writable_session(fake_team_repo):
    result = _run(fake_team_repo)

    assert result.returncode == 0, result.stderr
    assert fake_team_repo["state"].is_relative_to(fake_team_repo["root"])
    assert not fake_team_repo["state"].is_relative_to(fake_team_repo["session"])
    docker_log = fake_team_repo["log"].read_text(encoding="utf-8")
    assert f"{fake_team_repo['session']}:/testbed/.opencollab:rw" in docker_log
    assert f"{fake_team_repo['state']}:/testbed/.opencollab:rw" not in docker_log
    assert re.search(
        re.escape(str(fake_team_repo["state"]))
        + r"/internal-retirements-[0-9a-f]{32}\.jsonl:"
        + r"/run/opencollab-retirements-[0-9a-f]{32}\.jsonl:rw",
        docker_log,
    )
    assert "bounded-diff-command" not in docker_log
    assert "pause " in docker_log
    assert "cp " in docker_log and ":/testbed/. -" in docker_log


@pytest.mark.parametrize("image", ["--privileged", "-v", "bad image", "https://bad"])
def test_team_runner_rejects_docker_image_option_injection(fake_team_repo, image):
    result = subprocess.run(
        [
            "bash",
            str(fake_team_repo["runner"]),
            "--instance-file",
            str(fake_team_repo["instance"]),
            "--output",
            str(fake_team_repo["output"]),
            "--team-file",
            str(fake_team_repo["root"] / "configs" / "team.self.collab.yaml"),
            "--session-root",
            str(fake_team_repo["session"]),
            "--image",
            image,
        ],
        cwd=fake_team_repo["root"],
        env=fake_team_repo["env"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid Docker image reference" in result.stderr
    assert not fake_team_repo["log"].exists()


def test_team_runner_rejects_prompt_above_cli_bound_before_container_start(
    fake_team_repo,
):
    instance = json.loads(fake_team_repo["instance"].read_text(encoding="utf-8"))
    instance["problem_statement"] = "x" * (4 * 1024 * 1024)
    fake_team_repo["instance"].write_text(json.dumps(instance), encoding="utf-8")

    result = _run(fake_team_repo)

    assert result.returncode != 0
    assert "instance file failed bounded validation" in result.stderr
    assert not fake_team_repo["log"].exists()


def test_team_runner_long_instance_id_uses_bounded_nonce_pidfile(fake_team_repo):
    instance = json.loads(fake_team_repo["instance"].read_text(encoding="utf-8"))
    instance["instance_id"] = "a" * 240
    fake_team_repo["instance"].write_text(json.dumps(instance), encoding="utf-8")

    result = _run(fake_team_repo)

    assert result.returncode == 0, result.stderr
    docker_log = fake_team_repo["log"].read_text(encoding="utf-8")
    match = re.search(r"container_process_guard\.sh prepare (/tmp/\S+\.pid)", docker_log)
    assert match is not None
    assert len(Path(match.group(1)).name.encode("utf-8")) <= 255
    assert re.search(r"-[0-9a-f]{16}-[0-9a-f]{12}\.pid$", match.group(1))


def test_team_runner_name_conflict_never_removes_foreign_container(fake_team_repo):
    result = _run(fake_team_repo, env={"FAKE_RUN_CONFLICT": "1"})

    assert result.returncode == 125
    docker_log = fake_team_repo["log"].read_text(encoding="utf-8")
    assert "inspect --type container --format" in docker_log
    assert "rm -f" not in docker_log
    assert (fake_team_repo["root"] / "docker-state.exists").exists()
    assert (fake_team_repo["state"] / "team_container.owner").exists()


def test_team_runner_invalid_cid_removes_only_matching_labeled_container(
    fake_team_repo,
):
    result = _run(fake_team_repo, env={"FAKE_RUN_INVALID_CID": "1"})

    assert result.returncode != 0
    docker_log = fake_team_repo["log"].read_text(encoding="utf-8")
    assert "--label opencollab.harness.owner-token=" in docker_log
    assert "rm -f" in docker_log
    assert not (fake_team_repo["root"] / "docker-state.exists").exists()
