from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "start_team_run.sh"
BATCH_RUNNER = REPO_ROOT / "scripts" / "run_team_batch.sh"


def test_legacy_team_runner_help_names_current_safe_entrypoint() -> None:
    result = subprocess.run(
        [str(RUNNER), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "gen_prediction_workflow.py" in result.stdout
    assert "host-trusted patch-extraction" in result.stdout


def test_legacy_team_runner_fails_before_external_execution(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "docker-called"
    docker = fake_bin / "docker"
    docker.write_text(
        f"#!/bin/sh\ntouch {marker!s}\nexit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = subprocess.run(
        [str(RUNNER), "--instance-file", "missing", "--output", "missing"],
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 125
    assert "evaluation-integrity gate" in result.stderr
    assert not marker.exists()


def test_legacy_team_runner_never_claims_success() -> None:
    result = subprocess.run(
        [str(RUNNER)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 125
    assert "disabled" in result.stderr


def test_legacy_batch_runner_is_the_same_technical_gate() -> None:
    help_result = subprocess.run(
        [str(BATCH_RUNNER), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    run_result = subprocess.run(
        [str(BATCH_RUNNER), "--output", "ignored"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "swe_v1_prolite_runner.py" in help_result.stdout
    assert run_result.returncode == 125
    assert "disabled" in run_result.stderr
