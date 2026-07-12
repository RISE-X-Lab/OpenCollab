from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_openhands_cli.sh"
CONFIGURATION_KEYS = (
    "OPENCOLLAB_OPENHANDS_SITE",
    "OPENCOLLAB_PYDEPS",
    "OPENCOLLAB_REMOTE_REPO",
    "OPENCOLLAB_REMOTE_ROOT",
)


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in CONFIGURATION_KEYS:
        environment.pop(key, None)
    return environment


def test_openhands_launcher_fails_fast_without_runtime_configuration() -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        env=_clean_environment(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "OPENCOLLAB_REMOTE_REPO" in completed.stderr


def test_openhands_launcher_accepts_explicit_runtime_directories(tmp_path: Path) -> None:
    remote_repo = tmp_path / "repo"
    openhands_site = tmp_path / "openhands-site"
    pydeps = tmp_path / "pydeps"
    remote_repo.mkdir()
    (openhands_site / "openhands_cli").mkdir(parents=True)
    pydeps.mkdir()
    environment = _clean_environment()
    environment.update(
        {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "provider/model",
            "OPENCOLLAB_OPENHANDS_PYTHON": "/usr/bin/true",
            "OPENCOLLAB_OPENHANDS_SITE": str(openhands_site),
            "OPENCOLLAB_PYDEPS": str(pydeps),
            "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
        }
    )

    completed = subprocess.run(
        ["bash", str(SCRIPT), "--headless"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0


def test_openhands_launcher_derives_runtime_directories_from_configured_root(
    tmp_path: Path,
) -> None:
    remote_root = tmp_path / "remote-root"
    (remote_root / "tools" / "openhands-site" / "openhands_cli").mkdir(
        parents=True
    )
    (remote_root / "pydeps").mkdir()
    environment = _clean_environment()
    environment.update(
        {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "provider/model",
            "OPENCOLLAB_OPENHANDS_PYTHON": "/usr/bin/true",
            "OPENCOLLAB_REMOTE_REPO": str(remote_root),
            "OPENCOLLAB_REMOTE_ROOT": str(remote_root),
        }
    )

    completed = subprocess.run(
        ["bash", str(SCRIPT), "--headless"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
