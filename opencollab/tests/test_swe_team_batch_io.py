from __future__ import annotations

import fcntl
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

batch_io = importlib.import_module("scripts.swe_team_batch_io")


def _fake_swebench_package(tmp_path: Path) -> Path:
    package_root = tmp_path / "fake-packages"
    test_spec = package_root / "swebench" / "harness" / "test_spec"
    test_spec.mkdir(parents=True)
    for package in (
        package_root / "swebench",
        package_root / "swebench" / "harness",
        test_spec,
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "swebench" / "harness" / "utils.py").write_text(
        """
import json
import os


def load_swebench_dataset(dataset, split):
    return json.loads(os.environ["OC_TEST_DATASET"])
""",
        encoding="utf-8",
    )
    (test_spec / "test_spec.py").write_text(
        """
class _Spec:
    instance_image_key = "image:latest"


def make_test_spec(instance, namespace):
    return _Spec()
""",
        encoding="utf-8",
    )
    return package_root


@pytest.mark.parametrize(
    "instance_id",
    ["", ".", "..", "../escape", "nested/task", "task\trow", "task\u200drow"],
)
def test_batch_instance_id_rejects_path_and_tsv_injection(instance_id):
    with pytest.raises(ValueError):
        batch_io.validate_instance_id(instance_id)


def test_batch_prepare_rejects_symlinked_logs_dir_without_outside_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = tmp_path / "logs"
    logs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        batch_io.prepare_paths(tmp_path / "predictions.jsonl", logs)

    assert list(outside.iterdir()) == []


def test_batch_prepare_rejects_symlinked_output_parent_without_outside_write(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        batch_io.prepare_paths(linked / "predictions.jsonl", tmp_path / "logs")

    assert list(outside.iterdir()) == []


def test_batch_summary_append_rejects_final_symlink(tmp_path):
    _output, logs, summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    victim = tmp_path / "victim.tsv"
    victim.write_text("unchanged", encoding="utf-8")
    summary.unlink()
    summary.symlink_to(victim)

    with pytest.raises(OSError, match="regular"):
        batch_io.append_summary(
            summary,
            ["now", "task", "ok", "1", "2", "warn"],
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert logs.is_dir()


def test_batch_summary_lock_wait_is_bounded(tmp_path, monkeypatch):
    _output, _logs, summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    lock = summary.with_name(f".{summary.name}.lock")
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(batch_io, "LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring"):
            batch_io.append_summary(
                summary,
                ["now", "task", "ok", "1", "2", "warn"],
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_batch_log_rejects_symlink_without_starting_child(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    victim = tmp_path / "victim.log"
    victim.write_text("unchanged", encoding="utf-8")
    log = logs / "task.log"
    log.symlink_to(victim)
    sentinel = tmp_path / "started"

    with pytest.raises(OSError, match="regular or absent"):
        batch_io.run_with_log(
            log,
            [sys.executable, "-c", f"open({str(sentinel)!r}, 'w').write('x')"],
        )

    assert not sentinel.exists()
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_batch_log_captures_output_and_preserves_child_returncode(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    log = logs / "task.log"

    returncode = batch_io.run_with_log(
        log,
        [sys.executable, "-c", "import sys; print('captured'); raise SystemExit(7)"],
    )

    assert returncode == 7
    assert log.read_text(encoding="utf-8") == "captured\n"


def test_batch_script_checks_missing_start_and_has_column_fallback():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    ).read_text(encoding="utf-8")

    assert "--start-from instance was not found" in source
    assert "if command -v column" in source
    assert "display-summary --summary" in source
    assert "flock(fd, fcntl.LOCK_EX)" not in source


def test_batch_script_rejects_missing_start_from_in_dataset(tmp_path):
    package_root = _fake_swebench_package(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    env = os.environ.copy()
    env.update(
        {
            "OPENCOLLAB_EVAL_PYTHON": sys.executable,
            "OPENCOLLAB_SWEBENCH_NAMESPACE": "swebench",
            "PYTHONPATH": str(package_root),
            "OC_TEST_DATASET": json.dumps(
                [{"instance_id": "demo__task-1", "problem_statement": "fix"}]
            ),
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--logs-dir",
            str(tmp_path / "logs"),
            "--start-from",
            "demo__missing",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "--start-from instance was not found" in result.stderr


def test_batch_script_rejects_dataset_id_with_tsv_control(tmp_path):
    package_root = _fake_swebench_package(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    env = os.environ.copy()
    env.update(
        {
            "OPENCOLLAB_EVAL_PYTHON": sys.executable,
            "PYTHONPATH": str(package_root),
            "OC_TEST_DATASET": json.dumps(
                [{"instance_id": "demo\ttask", "problem_statement": "fix"}]
            ),
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--logs-dir",
            str(tmp_path / "logs"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert not list((tmp_path / "logs").glob("*.instance.json"))


def test_batch_io_cli_run_log_returns_child_status(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    script = Path(batch_io.__file__)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "run-log",
            "--log",
            str(logs / "cli.log"),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(9)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 9
