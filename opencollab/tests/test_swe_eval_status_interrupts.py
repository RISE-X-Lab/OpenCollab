from __future__ import annotations

from swe_eval_status_support import (
    _patch,
    _per_instance_run_kwargs,
    _spawn_term_ignoring_descendant,
    importlib,
    io,
    json,
    os,
    pytest,
    signal,
    subprocess,
    sys,
    threading,
    time,
)


def test_per_instance_owned_cleanup_defers_repeated_interrupts():
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    class DoubleInterruptDone:
        calls = 0

        def is_set(self):
            return self.calls >= 3

        def wait(self, timeout):
            self.calls += 1
            if self.calls <= 2:
                raise KeyboardInterrupt(f"cancel-{self.calls}")
            return True

    completed, interruption = runner._wait_for_owned_cleanup(
        DoubleInterruptDone(),
        timeout=0.2,
    )

    assert completed is True
    assert isinstance(interruption, KeyboardInterrupt)
    assert interruption.args == ("cancel-1",)


def test_per_instance_timeout_cleanup_re_raises_caller_interrupt_after_reap(
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    waits = []
    signals = []

    class CooperativeProcess:
        pid = 424245

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

    real_wait = runner._wait_for_owned_cleanup

    def interrupted_wait(done, *, timeout):
        completed, _interruption = real_wait(done, timeout=timeout)
        return completed, KeyboardInterrupt("caller cancelled during timeout cleanup")

    monkeypatch.setattr(runner, "_wait_for_owned_cleanup", interrupted_wait)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="caller cancelled"):
        runner.terminate_process_group(
            CooperativeProcess(),
            io.StringIO(),
            term_timeout=0.01,
            kill_timeout=0.01,
        )

    assert signals == [(424245, signal.SIGTERM)]
    assert len(waits) == 1
    assert 0 <= waits[0] <= 0.01


def test_per_instance_outer_timeout_reports_unreaped_cleanup(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    release = threading.Event()

    class StubbornProcess:
        pid = 424244

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *args, **kwargs: None)
    try:
        result = runner.run_one(**kwargs)
    finally:
        release.set()

    assert result == ("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)
    log = kwargs["work_dir"] / "command_logs" / "task-1.log"
    assert "technical cleanup failure" in log.read_text(encoding="utf-8")
    claim_path = runner._claim_path(kwargs["work_dir"], "task-1")
    assert claim_path.exists()
    assert json.loads(claim_path.read_text(encoding="utf-8"))["owner_token"]


def test_per_instance_wait_interrupt_terminates_child_and_re_raises(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    signals = []

    class InterruptedProcess:
        pid = 424247

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt("interrupt during outer wait")
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="outer wait"):
        runner.run_one(**kwargs)

    assert signals == [(424247, signal.SIGTERM)]
    assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()


def test_per_instance_main_interrupt_cancels_futures_and_terminates_registry(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text("[]", encoding="utf-8")
    predictions.write_text("", encoding="utf-8")
    signals = []
    pools = []

    class ActiveProcess:
        pid = 424248

        def wait(self, timeout=None):
            return 0

    class FakeFuture:
        cancelled = False

        def cancel(self):
            self.cancelled = True
            return True

    class FakePool:
        def __init__(self, max_workers):
            self.future = FakeFuture()
            self.shutdown_calls = []
            pools.append(self)

        def submit(self, fn, **kwargs):
            kwargs["active_processes"].add(ActiveProcess())
            return self.future

        def shutdown(self, *, wait, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr(runner, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(
        runner,
        "load_eval_queue",
        lambda *args, **kwargs: [
            (
                "task-1",
                "model",
                {
                    "instance_id": "task-1",
                    "record_id": "r1",
                    "patch_sha256": "a" * 64,
                },
                {"instance_id": "task-1", "model_patch": "+fix"},
            )
        ],
    )
    monkeypatch.setattr(
        runner,
        "as_completed",
        lambda futures: (_ for _ in ()).throw(KeyboardInterrupt("main interrupted")),
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
            "--work-dir",
            str(tmp_path / "eval"),
            "--run-id",
            "run",
        ],
    )

    with pytest.raises(KeyboardInterrupt, match="main interrupted"):
        runner.main()

    assert signals == [(424248, signal.SIGTERM)]
    assert pools[0].future.cancelled is True
    assert pools[0].shutdown_calls == [(False, True)]


def test_per_instance_stop_race_after_popen_self_terminates(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    stop_event = threading.Event()
    active_processes = runner.ActiveProcessRegistry()
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    kwargs.update(
        active_processes=active_processes,
        stop_event=stop_event,
    )
    popen_entered = threading.Event()
    release_popen = threading.Event()
    signals = []
    results = []

    class CooperativeProcess:
        pid = 424249

        def wait(self, timeout=None):
            return 0

    def gated_popen(*args, **kwargs):
        popen_entered.set()
        assert release_popen.wait(timeout=2)
        return CooperativeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", gated_popen)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    worker = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    worker.start()
    assert popen_entered.wait(timeout=2)
    stop_event.set()
    assert active_processes.terminate_all(io.StringIO()) is True
    release_popen.set()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert results == [("task-1", 130)]
    assert signals == [(424249, signal.SIGTERM)]


def test_per_instance_main_cleanup_failure_retains_residual_claim(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    process = _spawn_term_ignoring_descendant(tmp_path)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    stop_event = threading.Event()
    active_processes = runner.ActiveProcessRegistry()
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    kwargs.update(
        active_processes=active_processes,
        stop_event=stop_event,
    )
    results = []

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        runner,
        "process_start_identity",
        lambda pid: f"start-{pid}",
    )

    def incomplete_registry_cleanup(active_process, log_file):
        del log_file
        os.killpg(active_process.pid, signal.SIGTERM)
        return False

    monkeypatch.setattr(
        runner,
        "terminate_process_group",
        incomplete_registry_cleanup,
    )
    worker = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    worker.start()
    claim_path = runner._claim_path(kwargs["work_dir"], "task-1")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            claim = {}
        if claim.get("status") == "running":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("worker did not publish running claim")

    try:
        stop_event.set()
        assert active_processes.terminate_all(io.StringIO()) is False
        worker.join(timeout=2)
        retained = json.loads(claim_path.read_text(encoding="utf-8"))

        assert worker.is_alive() is False
        assert results == [("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)]
        assert retained["status"] == "cleanup_failed"
        assert retained["evaluator_pgid"] == process.pid
        assert retained["lease_until_ns"] > time.time_ns()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_per_instance_main_continues_after_future_exception(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    seen = []
    predictions = []
    for task in ("task-1", "task-2"):
        prediction = {
            "instance_id": task,
            "record_id": f"{task}-r1",
            "model_name_or_path": "model",
            "model_patch": _patch(f"+{task}\n"),
        }
        predictions.append((task, "model", runner.prediction_identity(prediction), prediction))

    monkeypatch.setattr(runner, "load_eval_queue", lambda *args, **kwargs: predictions)

    def fake_run_one(**kwargs):
        seen.append(kwargs["iid"])
        if kwargs["iid"] == "task-1":
            raise RuntimeError("unexpected worker crash")
        return kwargs["iid"], 0

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(tmp_path / "dataset.json"),
            "--predictions",
            str(tmp_path / "predictions.jsonl"),
            "--work-dir",
            str(tmp_path / "eval"),
            "--run-id",
            "run",
            "--workers",
            "2",
        ],
    )

    assert runner.main() == 1
    assert sorted(seen) == ["task-1", "task-2"]
