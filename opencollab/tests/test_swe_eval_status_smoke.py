from __future__ import annotations

from swe_eval_status_support import (
    Path,
    SimpleNamespace,
    _strict_modern_prediction,
    hashlib,
    importlib,
    json,
    os,
    pytest,
    records_mod,
    signal,
    subprocess,
    sys,
    time,
)


def test_docker_eval_wrapper_accepts_positive_fractional_timeout(monkeypatch):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_API_TIMEOUT", "2.5")
    monkeypatch.setattr(
        wrapper,
        "_original_from_env",
        lambda *args, **kwargs: captured.update(kwargs) or "client",
    )

    result = wrapper._from_env_with_timeout(version="auto")

    assert result == "client"
    assert captured == {"version": "auto", "timeout": 2.5}


@pytest.mark.parametrize(
    ("module_name", "blocked_prefix"),
    [
        ("scripts.run_swebench_eval_with_docker_timeout", "docker"),
        ("scripts.run_swebench_smoke_batch", "swebench.harness"),
    ],
)
def test_smoke_helpers_defer_optional_dependency_imports(
    module_name,
    blocked_prefix,
):
    code = f"""
import builtins
import importlib

original_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == {blocked_prefix!r} or name.startswith({blocked_prefix!r} + "."):
        raise ModuleNotFoundError(name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
importlib.import_module({module_name!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_docker_eval_wrapper_blank_primary_falls_back_to_client_timeout(monkeypatch):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_API_TIMEOUT", " ")
    monkeypatch.setenv("DOCKER_CLIENT_TIMEOUT", "3")
    monkeypatch.setattr(
        wrapper,
        "_original_from_env",
        lambda *args, **kwargs: captured.update(kwargs) or "client",
    )

    wrapper._from_env_with_timeout()

    assert captured["timeout"] == 3.0


@pytest.mark.parametrize("value", ["bad", "0", "-1", "nan", "inf"])
def test_docker_eval_timeout_rejects_non_positive_or_non_finite(value):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    with pytest.raises(ValueError, match="positive finite number"):
        wrapper.positive_timeout_seconds(value, name="TIMEOUT")
    with pytest.raises(ValueError, match="positive finite number"):
        runner.positive_timeout_seconds(value, name="TIMEOUT")


def test_smoke_batch_returns_failure_when_generator_fails(monkeypatch, tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.delenv("DOCKER_DEFAULT_PLATFORM", raising=False)
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir()
    (instances_dir / "task.json").write_text(
        json.dumps({"instance_id": "task-1"}),
        encoding="utf-8",
    )
    observed = {}

    def fake_make_test_spec(instance, namespace, arch):
        observed["arch"] = arch
        return SimpleNamespace(instance_image_key="image")

    monkeypatch.setattr(driver, "make_test_spec", fake_make_test_spec)

    def fake_run(*args, **kwargs):
        observed["platform"] = kwargs["env"]["DOCKER_DEFAULT_PLATFORM"]
        return 9, "failed"

    monkeypatch.setattr(driver, "_run_generator", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_smoke_batch.py",
            "--instances-dir",
            str(instances_dir),
            "--output-dir",
            str(tmp_path / "output"),
            "--model-name",
            "test-model",
            "--arch",
            "arm64",
        ],
    )

    assert driver.main() == 1
    assert observed == {"arch": "arm64", "platform": "linux/arm64"}


def test_smoke_generator_outer_timeout_kills_term_ignoring_descendant(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(started)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not p.exists()]; "
        "time.sleep(10)"
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=0.3,
        spawn_timeout=0.5,
        term_timeout=0.1,
        kill_timeout=0.5,
    )

    assert returncode in {124, driver.TECHNICAL_EXIT_CODE}
    assert "timeout" in reason or "timed out" in reason
    assert started.exists()
    time.sleep(0.9)
    assert not finished.exists()


def test_smoke_generator_normal_exit_kills_residual_descendant(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    ready = tmp_path / "normal-exit-child.ready"
    finished = tmp_path / "normal-exit-child.finished"
    child_code = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "[(time.sleep(0.01)) for _ in range(200) if not p.exists()]"
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", leader_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=2,
        spawn_timeout=0.5,
        term_timeout=0.05,
        kill_timeout=1.0,
    )

    assert returncode == 0
    assert reason == ""
    assert ready.exists()
    time.sleep(0.9)
    assert not finished.exists()


def test_smoke_generator_normal_exit_cleanup_failure_is_technical(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(
        driver,
        "_ensure_process_tree_quiesced_after_wait",
        lambda *args, **kwargs: False,
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", "raise SystemExit(0)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=2,
        spawn_timeout=0.5,
        term_timeout=0.05,
        kill_timeout=0.5,
    )

    assert returncode == driver.TECHNICAL_EXIT_CODE
    assert "leader exited" in reason


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_smoke_generator_signal_cleans_child_before_parent_exits(tmp_path, signum):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    generator_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(started)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not p.exists()]; "
        "time.sleep(10)"
    )
    script = f"""
import os, sys
from pathlib import Path
from scripts.run_swebench_smoke_batch import _run_generator
_run_generator(
    [sys.executable, "-c", {generator_code!r}],
    cwd=Path({str(tmp_path)!r}),
    env=os.environ.copy(),
    outer_timeout=10,
    spawn_timeout=1,
    term_timeout=0.1,
    kill_timeout=0.5,
)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    parent = subprocess.Popen([sys.executable, "-c", script], env=env)
    for _ in range(300):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()

    parent.send_signal(signum)
    parent.wait(timeout=5)

    time.sleep(0.9)
    assert not finished.exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(91)])
def test_smoke_generator_interruption_immediately_after_worker_start_cleans(
    monkeypatch,
    tmp_path,
    interruption,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    real_wait = driver._wait_event_resisting_interrupt
    calls = 0

    def interrupt_first_wait(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise interruption
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(driver, "_wait_event_resisting_interrupt", interrupt_first_wait)

    with pytest.raises(type(interruption)):
        driver._run_generator(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            outer_timeout=10,
            spawn_timeout=1,
            term_timeout=0.1,
            kill_timeout=0.5,
        )

    assert calls >= 2


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "inf"),
        ("--timeout", "0"),
        ("--outer-timeout", "nan"),
        ("--spawn-timeout", "-1"),
    ],
)
def test_smoke_batch_rejects_invalid_timeout_before_io(
    monkeypatch,
    tmp_path,
    flag,
    value,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_smoke_batch.py",
            "--instances-dir",
            str(tmp_path / "missing"),
            "--output-dir",
            str(tmp_path / "output"),
            "--model-name",
            "test-model",
            flag,
            value,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        driver.main()

    assert exc_info.value.code == 2


def test_smoke_batch_patch_reader_rejects_truncated_tail(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+fixed"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "current",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "current",
                    "patch_sha256": patch_sha,
                    "workflow_status": "done",
                    "runner_returncode": 0,
                },
            }
        )
        + "\n"
        + '{"instance_id":',
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="invalid JSONL"):
        driver._prediction_has_patch(output, "task-1")


def test_smoke_batch_patch_reader_rejects_non_object_json_rows(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+fixed"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        "[]\nnull\n"
        + json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "current",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "current",
                    "patch_sha256": patch_sha,
                    "workflow_status": "done",
                    "runner_returncode": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="must be an object"):
        driver._prediction_has_patch(output, "task-1")


def test_smoke_batch_patch_reader_uses_latest_candidate(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+old"})
        + "\n"
        + json.dumps({"instance_id": "task-1", "model_patch": ""})
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_reruns_legacy_patch_without_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+legacy"}) + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_rejects_failed_embedded_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "failed-record",
                "model_patch": "+partial",
                "workflow_metric": {
                    "record_id": "failed-record",
                    "workflow_status": "error",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_accepts_timeout_patch_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    prediction = _strict_modern_prediction(
        status="done_with_timeout_patch",
        returncode=124,
    )
    output.write_text(
        json.dumps(prediction) + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is True


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("done", 1), ("done_with_timeout_patch", 1), ("done_with_timeout_patch", 0)],
)
def test_smoke_batch_patch_reader_reruns_status_returncode_mismatch(
    tmp_path,
    status,
    returncode,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    prediction = _strict_modern_prediction(status=status, returncode=returncode)
    output.write_text(
        json.dumps(prediction) + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_rejects_metric_for_different_patch(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "mismatch",
                "model_patch": "+current",
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "mismatch",
                    "patch_sha256": "0" * 64,
                    "workflow_status": "done",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False
