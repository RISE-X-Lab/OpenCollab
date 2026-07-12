from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _proven_submission_integrity,
    _remote_namespace,
    _write_jsonl,
    pytest,
    subprocess,
    sys,
    threading,
)


def test_remote_spawn_guard_does_not_block_sigterm_in_exec_child(tmp_path):
    namespace = _remote_namespace(tmp_path)
    spawn_signal_state = namespace["block_spawn_signals"]()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    namespace["ACTIVE_CHILD_PGIDS"].add(proc.pid)
    namespace["restore_spawn_signals"](spawn_signal_state)
    try:
        namespace["os"].killpg(proc.pid, namespace["signal"].SIGTERM)
        returncode = proc.wait(timeout=1)
    finally:
        namespace["ACTIVE_CHILD_PGIDS"].discard(proc.pid)
        if proc.poll() is None:
            namespace["os"].killpg(proc.pid, namespace["signal"].SIGKILL)
            proc.wait(timeout=1)

    assert returncode == -namespace["signal"].SIGTERM


def test_remote_active_child_cleanup_escalates_to_kill_and_proves_absence(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    namespace["ACTIVE_CHILD_PGIDS"].add(424299)
    monkeypatch.setitem(namespace, "PROCESS_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setitem(namespace, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.0)
    signals = []
    probes = 0

    def group_exists(_pgid):
        nonlocal probes
        probes += 1
        return probes == 1

    monkeypatch.setitem(namespace, "process_group_exists", group_exists)
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    assert namespace["terminate_active_children"]() is True
    assert signals == [
        (424299, namespace["signal"].SIGTERM),
        (424299, namespace["signal"].SIGKILL),
    ]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()


def test_generation_timeout_recovers_completed_candidate(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
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
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["returncode"] == 124
    assert result["timed_out"] is True


def test_generation_timeout_reports_stubborn_kill_reap(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    release = threading.Event()
    consumer_started = threading.Event()
    task = "task-1"
    signals = []

    class StubbornProcess:
        pid = 424243

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
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    try:
        result = namespace["generation_for_task"]({"instance_id": task})
    finally:
        release.set()

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert signals == [
        (424243, namespace["signal"].SIGTERM),
        (424243, namespace["signal"].SIGKILL),
    ]
    assert consumer_started.wait(timeout=0.2)


def test_generation_normal_exit_cleanup_failure_is_technical(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"

    class ReapedLeader:
        pid = 424262

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: False

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert 424262 in namespace["ACTIVE_CHILD_PGIDS"]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_generation_wait_system_exit_terminates_child_and_re_raises(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class InterruptedProcess:
        pid = 424250

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise SystemExit(77)
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
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 77
    assert signals == [(424250, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_generation_spawn_registration_precedes_pending_signal_restore(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class SpawnedProcess:
        pid = 424252

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: SpawnedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    real_restore = namespace["restore_spawn_signals"]

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert 424252 in namespace["ACTIVE_CHILD_PGIDS"]
        raise SystemExit(78)

    namespace["restore_spawn_signals"] = restore_then_interrupt

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 78
    assert signals == [(424252, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()
    assert namespace["ACTIVE_FIFO_PATHS"] == set()
