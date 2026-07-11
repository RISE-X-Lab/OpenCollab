from __future__ import annotations

from swe_eval_status_support import (
    Path,
    _patch,
    _write_jsonl,
    json,
    os,
    pytest,
    row_patch_sha,
    subprocess,
    sys,
)


def test_wave_watchdog_summarizes_runs_config_without_actions(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    runs_config = tmp_path / "runs.json"
    runs_config.write_text(
        json.dumps(
            [
                {
                    "name": "local",
                    "base_run_dir": str(run_dir),
                    "tasks": ["task-1"],
                    "workflow": "single-agent",
                }
            ]
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(runs_config)],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(result.stdout)
    assert summary["schema"] == "opencollab.swe_wave_status.v1"
    assert summary["totals"]["ready_for_eval"] == 1
    assert summary["runs"][0]["tasks"][0]["state"] == "ready_for_eval"


@pytest.mark.parametrize(
    "bad_run",
    [
        "not-an-object",
        {"base_run_dir": "", "tasks": []},
        {"base_run_dir": "relative/run", "tasks": []},
        {"base_run_dir": "/definitely/missing/opencollab-run", "tasks": []},
        {"base_run_dir": "/tmp", "side_name": "../escape", "tasks": []},
        {"base_run_dir": "/tmp", "tasks": "task-1"},
        {"base_run_dir": "/tmp", "tasks": ["task-1", 2]},
    ],
)
def test_wave_watchdog_bad_run_schema_is_incomplete_and_exit_two(tmp_path, bad_run):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text(json.dumps([bad_run]), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    summary = json.loads(result.stdout)
    assert summary["complete"] is False
    assert summary["input_errors"]
    assert summary["totals"]["invalid_runs"] == 1


def test_wave_watchdog_rejects_symlinked_base_run_directory(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    real = tmp_path / "real-run"
    real.mkdir()
    linked = tmp_path / "linked-run"
    linked.symlink_to(real, target_is_directory=True)
    config = tmp_path / "runs.json"
    config.write_text(
        json.dumps([{"base_run_dir": str(linked), "tasks": []}]),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    summary = json.loads(result.stdout)
    assert summary["complete"] is False
    assert summary["runs"][0]["config_errors"]


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_wave_watchdog_rejects_unsafe_runs_config_without_blocking(tmp_path, kind):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    if kind == "fifo":
        os.mkfifo(config)
    else:
        real = tmp_path / "real.json"
        real.write_text("[]", encoding="utf-8")
        config.symlink_to(real)

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert "bounded regular JSON file" in result.stderr


def test_wave_watchdog_rejects_symlinked_runs_config_ancestor(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "runs.json").write_text("[]", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(linked / "runs.json")],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    assert "bounded regular JSON file" in result.stderr


@pytest.mark.parametrize("flag", ["--json-output", "--markdown-output"])
@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_wave_watchdog_rejects_unsafe_output_without_blocking(
    tmp_path,
    flag,
    kind,
):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text("[]", encoding="utf-8")
    output = tmp_path / "summary.out"
    real = None
    if kind == "fifo":
        os.mkfifo(output)
    else:
        real = tmp_path / "real.out"
        real.write_text("original", encoding="utf-8")
        output.symlink_to(real)

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runs-config",
            str(config),
            flag,
            str(output),
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert "output path is not a regular file" in result.stderr
    if real is not None:
        assert real.read_text(encoding="utf-8") == "original"


def test_wave_watchdog_rejects_symlinked_output_parent(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text("[]", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runs-config",
            str(config),
            "--json-output",
            str(linked / "summary.json"),
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert list(outside.iterdir()) == []
