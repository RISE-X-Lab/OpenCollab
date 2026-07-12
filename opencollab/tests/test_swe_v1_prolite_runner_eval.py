from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    Path,
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
    json,
    os,
    pytest,
    subprocess,
    threading,
)


def _owned_eval_marker(namespace, marker_path, container_id, container_name, *, state="active"):
    cleanup = namespace["remote_cleanup"]
    marker_path.write_text(
        json.dumps(
            {
                "schema": cleanup.EVAL_CONTAINER_SCHEMA,
                "state": state,
                "container_name": container_name,
                "container_id": container_id if state == "active" else "",
                "owner_nonce": namespace["owner_nonce"],
                "owner_label": cleanup.EVAL_OWNER_LABEL,
                "owner_schema_label": cleanup.EVAL_SCHEMA_LABEL,
                "owner_schema": cleanup.EVAL_SCHEMA_LABEL_VALUE,
            }
        ),
        encoding="utf-8",
    )


def _bypass_container_binding(namespace):
    namespace["bind_eval_container_marker"] = lambda *args, **kwargs: {
        "ok": True,
        "container_id": "f" * 64,
    }


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_eval_container_unsafe_exit_artifact_is_technical_without_blocking(
    monkeypatch,
    tmp_path,
    kind,
):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    namespace = _remote_namespace(tmp_path)
    _bypass_container_binding(namespace)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)

    class FinishedProcess:
        pid = 424270

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, *args, **kwargs):
        mount = next(item for item in command if str(item).endswith(":/eval_output"))
        output_dir = Path(str(mount).removesuffix(":/eval_output"))
        for name in (
            "base_commit.exit",
            "service_bootstrap.exit",
            "before_repo.exit",
            "post_before_base.exit",
            "model_patch.exit",
            "test_patch.exit",
            "f2p.exit",
            "p2p.exit",
        ):
            (output_dir / name).write_text("0\n", encoding="ascii")
        unsafe = output_dir / "f2p.exit"
        unsafe.unlink()
        if kind == "symlink":
            target = output_dir / "attacker.exit"
            target.write_text("0\n", encoding="ascii")
            unsafe.symlink_to(target)
        else:
            os.mkfifo(unsafe)
        (output_dir / "f2p.log").write_text("", encoding="utf-8")
        (output_dir / "p2p.log").write_text("", encoding="utf-8")
        return FinishedProcess()

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: True

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert "unsafe_or_missing_output_artifact" in result["summary"]["technical_reasons"]
    assert any(error.startswith("unsafe:f2p.exit") for error in result["summary"]["output_artifact_errors"])


def test_eval_popen_failure_clears_only_verified_pending_marker(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-start-failure"
    _seed_remote_completed_generation(namespace, task)
    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    eval_dir = namespace["base_run_dir"] / task / "official_eval_v1_prolite26_35_20260707"
    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["container_cleanup"]["status"] == "pending_marker_removed"
    assert not (eval_dir / "container.marker.json").exists()
    assert not (eval_dir / "container.cid").exists()


def test_pending_marker_with_wrong_owner_is_preserved(tmp_path):
    namespace = _remote_namespace(tmp_path)
    marker = tmp_path / "container.marker.json"
    cidfile = tmp_path / "container.cid"
    _owned_eval_marker(
        namespace,
        marker,
        "",
        "opencollab-prolite-foreign",
        state="pending",
    )
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["owner_nonce"] = "0" * 32
    marker.write_text(json.dumps(value), encoding="utf-8")

    result = namespace["clear_pending_eval_marker"](
        cidfile,
        marker,
        "opencollab-prolite-foreign",
    )

    assert result["ok"] is False
    assert result["status"] == "pending_marker_ownership_unproven"
    assert marker.exists()


def test_eval_timeout_returns_technical_result(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    _bypass_container_binding(namespace)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
            }
        ],
    )

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert "docker_exit" in result["summary"]["technical_reasons"]


def test_eval_timeout_reports_stubborn_kill_reap(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    _bypass_container_binding(namespace)
    release = threading.Event()
    consumer_started = threading.Event()
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
            }
        ],
    )

    class StubbornProcess:
        pid = 424244

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    try:
        result = namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": ["tests/test_a.py::test_a"],
                "repo_language": "python",
            }
        )
    finally:
        release.set()

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["cleanup_quiesced"] is False
    assert result["summary"]["docker_exit"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert "process_cleanup" in result["summary"]["technical_reasons"]
    assert consumer_started.wait(timeout=0.2)


def test_eval_normal_exit_cleanup_failure_is_technical(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    _bypass_container_binding(namespace)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
            }
        ],
    )

    class ReapedLeader:
        pid = 424263

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: False
    namespace["cleanup_eval_container"] = lambda *args: {
        "ok": True,
        "status": "removed",
    }

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["cleanup_quiesced"] is False
    assert result["summary"]["docker_exit"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert "process_cleanup" in result["summary"]["technical_reasons"]
    assert 424263 in namespace["ACTIVE_CHILD_PGIDS"]


def test_eval_timeout_force_removes_container_from_cidfile(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
            }
        ],
    )
    container_id = "a" * 64
    cleanup_calls = []
    docker_commands = []

    class TimedOutProcess:
        pid = 424255

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    def fake_popen(command, *args, **kwargs):
        docker_commands.append(command)
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text(container_id, encoding="utf-8")
        return TimedOutProcess()

    def fake_run(command, timeout=60):
        cleanup_calls.append((command, timeout))
        if command[1] == "inspect":
            return {"returncode": 1, "stdout": "", "stderr": "No such container"}
        return {"returncode": 0, "stdout": container_id, "stderr": ""}

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["run"] = fake_run

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    network_index = docker_commands[0].index("--network")
    assert docker_commands[0][network_index + 1] == "none"
    assert result["summary"]["container_cleanup"]["status"] == "all_references_absent"
    assert any(call[0][1] == "inspect" for call in cleanup_calls)


def test_container_cleanup_failure_preserves_recovery_markers(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_id = "b" * 64
    container_name = "opencollab-prolite-test"
    cidfile.write_text(container_id, encoding="utf-8")
    _owned_eval_marker(
        namespace,
        marker_path,
        container_id,
        container_name,
    )
    namespace["run"] = lambda command, timeout=60: {
        "returncode": 125,
        "stdout": "",
        "stderr": "docker daemon unavailable",
    }

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is False
    assert result["status"] == "inspect_failed"
    assert cidfile.exists()
    assert marker_path.exists()


def test_container_cleanup_uses_name_before_cidfile_exists(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-late-cid"
    _owned_eval_marker(
        namespace,
        marker_path,
        "",
        container_name,
        state="pending",
    )
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        if command[1] == "inspect":
            return {"returncode": 1, "stdout": "", "stderr": "No such container"}
        return {"returncode": 0, "stdout": container_name, "stderr": ""}

    namespace["run"] = fake_run

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is False
    assert result["status"] == "ownership_unproven"
    assert calls == []
    assert marker_path.exists()


def test_container_cleanup_preserves_markers_until_every_reference_is_absent(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_id = "c" * 64
    container_name = "opencollab-prolite-still-present"
    cidfile.write_text(container_id, encoding="utf-8")
    _owned_eval_marker(
        namespace,
        marker_path,
        container_id,
        container_name,
    )

    inspect_calls = 0

    def fake_run(command, timeout=60):
        if command[1] == "inspect":
            nonlocal inspect_calls
            inspect_calls += 1
            return {
                "returncode": 0,
                "stdout": (f"{container_id}\t{namespace['owner_nonce']}\tdirect-eval-v1"),
                "stderr": "",
            }
        return {"returncode": 0, "stdout": "", "stderr": ""}

    namespace["run"] = fake_run

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is False
    assert cidfile.exists()
    assert marker_path.exists()


def test_eval_wait_interrupt_terminates_child_and_re_raises(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    _bypass_container_binding(namespace)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
            }
        ],
    )
    signals = []

    class InterruptedProcess:
        pid = 424251

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt("eval interrupted")
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}

    with pytest.raises(KeyboardInterrupt, match="eval interrupted"):
        namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": ["tests/test_a.py::test_a"],
                "repo_language": "python",
            }
        )

    assert signals == [(424251, namespace["signal"].SIGTERM)]
