from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    SimpleNamespace,
    _remote_namespace,
    _spawn_normal_exit_with_term_ignoring_descendant,
    _spawn_term_ignoring_descendant,
    json,
    pytest,
    runner,
    shlex,
    subprocess,
    threading,
)


def test_remote_cleanup_ps_scan_and_container_markers_are_bounded(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
    )
    remote_parts = shlex.split(calls[0][0][-1])

    assert result["returncode"] == 0
    assert remote_parts[:4] == ["env", "PYTHONPATH=/remote/run/_runtime/repo/opencollab", "python3", "-m"]
    assert remote_parts[4:] == [
        "opencollab.harness.swe_v1_remote_cleanup",
        "/remote/run",
    ]
    assert "-c" not in remote_parts


def test_remote_cleanup_uses_exact_nonce_token_not_run_path_substring(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run1",
    )
    remote_parts = shlex.split(calls[0][-1])

    assert remote_parts[-2:] == [
        "opencollab.harness.swe_v1_remote_cleanup",
        "/remote/run1",
    ]
    assert "-c" not in remote_parts


def test_remote_cleanup_scan_failures_are_reported_as_technical(monkeypatch):
    payload = {
        "ok": False,
        "status": "technical_cleanup_failed",
        "scan_errors": ["TimeoutExpired('ps', 5)", "TimeoutExpired('ps', 5)"],
        "containers": [],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=3,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
    )

    assert result["returncode"] == 3
    assert result["detail"]["ok"] is False
    assert result["detail"]["status"] == "technical_cleanup_failed"


def test_local_remote_wrapper_kill_reap_is_bounded_and_drained(monkeypatch):
    release = threading.Event()
    consumer_started = threading.Event()
    consumer_finished = threading.Event()
    calls = []
    signals = []

    class StubbornProcess:
        pid = 424245

        def communicate(self, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            consumer_finished.set()
            return "", ""

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    quiesced = runner.terminate_local_process_group(
        StubbornProcess(),
        term_timeout=0.001,
        kill_timeout=0.001,
    )

    assert quiesced is False
    assert signals == [
        (424245, runner.signal.SIGTERM),
        (424245, runner.signal.SIGKILL),
    ]
    assert len(calls) >= 2
    assert all(0 <= timeout <= 0.001 for timeout in calls[:2])
    assert consumer_started.wait(timeout=0.2)
    release.set()
    assert consumer_finished.wait(timeout=0.2)


def test_remote_cleanup_kills_descendant_after_leader_exits(tmp_path):
    namespace = _remote_namespace(tmp_path)
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = namespace["terminate_process_group_bounded"](
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert namespace["process_group_exists"](process.pid) is False
    finally:
        try:
            namespace["os"].killpg(process.pid, namespace["signal"].SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_remote_normal_exit_cleans_residual_descendants(tmp_path):
    namespace = _remote_namespace(tmp_path)
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    try:
        assert process.wait(timeout=2) == 0
        assert namespace["process_group_exists"](process.pid) is True

        quiesced = namespace["ensure_process_group_quiesced_after_wait"](
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert namespace["process_group_exists"](process.pid) is False
    finally:
        try:
            namespace["os"].killpg(process.pid, namespace["signal"].SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_local_cleanup_kills_descendant_after_leader_exits(tmp_path):
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = runner.terminate_local_process_group(
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert runner._local_process_group_exists(process.pid) is False
    finally:
        try:
            runner.os.killpg(process.pid, runner.signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_local_normal_exit_cleans_residual_descendants(tmp_path):
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    try:
        assert process.wait(timeout=2) == 0
        assert runner._local_process_group_exists(process.pid) is True

        quiesced = runner._ensure_local_process_group_quiesced_after_wait(
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert runner._local_process_group_exists(process.pid) is False
    finally:
        try:
            runner.os.killpg(process.pid, runner.signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_local_remote_timeout_cleanup_re_raises_interrupt_after_drain(monkeypatch):
    calls = []
    signals = []

    class CooperativeProcess:
        pid = 424246

        def communicate(self, timeout=None):
            calls.append(timeout)
            return "", ""

    real_wait = runner._wait_for_owned_local_cleanup

    def interrupted_wait(done, *, timeout):
        completed, _interruption = real_wait(done, timeout=timeout)
        return completed, KeyboardInterrupt("caller cancelled during remote cleanup")

    monkeypatch.setattr(runner, "_wait_for_owned_local_cleanup", interrupted_wait)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="caller cancelled"):
        runner.terminate_local_process_group(
            CooperativeProcess(),
            term_timeout=0.01,
            kill_timeout=0.01,
        )

    assert signals == [(424246, runner.signal.SIGTERM)]
    assert len(calls) == 1
    assert 0 <= calls[0] <= 0.01


def test_run_remote_pending_system_exit_cleans_remote_and_local_process(
    monkeypatch,
    tmp_path,
):
    process_calls = []
    signals = []
    remote_cleanup_calls = []

    class SpawnedProcess:
        pid = 424254
        returncode = 0

        def communicate(self, input=None, timeout=None):
            process_calls.append((input, timeout))
            return "", ""

    process = SpawnedProcess()
    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: {"remote_proxy_base_url": "http://127.0.0.1:18788"},
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        runner,
        "terminate_remote_run",
        lambda **kwargs: remote_cleanup_calls.append(kwargs) or {"returncode": 0, "detail": {}},
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    real_restore = runner._restore_local_spawn_signals

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        raise SystemExit(80)

    monkeypatch.setattr(runner, "_restore_local_spawn_signals", restore_then_interrupt)
    args = SimpleNamespace(
        ssh_command="ssh",
        host="host",
        local_proxy_base_url="http://127.0.0.1:8878",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=True,
        no_sync_runtime=True,
        remote_runtime_repo="/remote/repo",
        proxy_env_file=tmp_path / "proxy.env",
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
        model_name="model",
        session_prefix="test",
        image_repository="registry.example/swebench",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=10,
    )

    with pytest.raises(SystemExit) as exc:
        runner.run_remote(args)

    assert exc.value.code == 80
    assert len(remote_cleanup_calls) == 1
    assert signals == [(424254, runner.signal.SIGTERM)]
    assert len(process_calls) == 1
    assert process_calls[0][0] is None
    assert 0 <= process_calls[0][1] <= runner.LOCAL_PROCESS_TERM_GRACE_SECONDS


def test_run_remote_normal_ssh_exit_cleanup_failure_is_technical(
    monkeypatch,
    tmp_path,
):
    class ReapedLeader:
        pid = 424264
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return json.dumps({"status": "done"}), ""

    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: {"remote_proxy_base_url": "http://127.0.0.1:18788"},
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pgid: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: (
            {
                "ok": False,
                "remote": {"returncode": 3},
                "local_cleanup_quiesced": False,
            },
            None,
        ),
    )
    args = SimpleNamespace(
        ssh_command="ssh",
        host="host",
        local_proxy_base_url="http://127.0.0.1:8878",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=True,
        no_sync_runtime=True,
        remote_runtime_repo="/remote/repo",
        proxy_env_file=tmp_path / "proxy.env",
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
        model_name="model",
        session_prefix="test",
        image_repository="registry.example/swebench",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=10,
    )

    with pytest.raises(RuntimeError, match="technical cleanup failure"):
        runner.run_remote(args)


def test_composite_cleanup_marks_remote_cleanup_failure(monkeypatch):
    class CooperativeProcess:
        pid = 424256

        def communicate(self, input=None, timeout=None):
            return "", ""

    monkeypatch.setattr(
        runner,
        "terminate_remote_run",
        lambda **kwargs: {
            "returncode": 3,
            "detail": {"ok": False, "status": "technical_cleanup_failed"},
        },
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *args, **kwargs: None)

    cleanup, interruption = runner._cleanup_remote_execution(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
        proc=CooperativeProcess(),
    )

    assert interruption is None
    assert cleanup["ok"] is False
    assert cleanup["remote"]["returncode"] == 3
    assert cleanup["local_cleanup_quiesced"] is True


def test_remote_owned_cleanup_defers_repeated_interrupts(tmp_path):
    namespace = _remote_namespace(tmp_path)

    class DoubleInterruptDone:
        calls = 0

        def is_set(self):
            return self.calls >= 3

        def wait(self, timeout):
            self.calls += 1
            if self.calls <= 2:
                raise KeyboardInterrupt(f"cancel-{self.calls}")
            return True

    completed, interruption = namespace["wait_for_owned_cleanup"](
        DoubleInterruptDone(),
        0.2,
    )

    assert completed is True
    assert isinstance(interruption, KeyboardInterrupt)
    assert interruption.args == ("cancel-1",)
