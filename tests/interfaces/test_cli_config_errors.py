"""Configuration failures must be actionable at both CLI entry points."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click import unstyle

from opencollab.bootstrap.config import MAX_DOTENV_BYTES, load_dotenv
from tests.support.paths import PACKAGE_ROOT


@pytest.mark.parametrize(
    ("payload", "reason"),
    [(b"KEY=value\n\xff", "encoding error: expected UTF-8"),
     (b"X" * (MAX_DOTENV_BYTES + 1), f"exceeds {MAX_DOTENV_BYTES}-byte limit")],
    ids=["invalid-utf8", "oversized"],
)
def test_dotenv_error_identifies_file_and_reason(tmp_path, payload, reason):
    config = tmp_path / "broken.env"
    config.write_bytes(payload)
    with pytest.raises(ValueError) as error:
        load_dotenv(str(config))
    assert str(config) in str(error.value)
    assert reason in str(error.value)


@pytest.mark.parametrize("command", [[], ["workflow", "run", "duo"]])
@pytest.mark.parametrize("terminal_width", [80, 240])
@pytest.mark.parametrize("github_actions", ["", "true"], ids=["local", "github-actions"])
@pytest.mark.parametrize(
    ("payload", "reason"),
    [(b"KEY=value\n\xff", "encoding error: expected UTF-8"),
     (b"X" * (MAX_DOTENV_BYTES + 1), f"exceeds {MAX_DOTENV_BYTES}-byte limit")],
    ids=["invalid-utf8", "oversized"],
)
def test_cli_reports_config_error_without_traceback(
    tmp_path, command, terminal_width, github_actions, payload, reason,
):
    config = tmp_path / "broken.env"
    config.write_bytes(payload)
    env = dict(os.environ)
    env.update(
        OPENCOLLAB_CONFIG_FILE=str(config),
        OPENCOLLAB_WORKFLOWS_DIR=str(tmp_path / "workflows"),
        PYTHONPATH=str(PACKAGE_ROOT),
        NO_COLOR="1",
        TERM="dumb",
        COLUMNS=str(terminal_width),
        TERMINAL_WIDTH=str(terminal_width),
        GITHUB_ACTIONS=github_actions,
    )
    result = subprocess.run(
        [sys.executable, "-m", "opencollab", *command, "--workspace", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    output = unstyle(result.stdout + result.stderr)
    # Rich may wrap words or paths across panel rows. Compare diagnostic
    # content independently of whitespace and the panel's vertical borders.
    diagnostic = "".join(output.replace("\u2502", "").split())
    assert result.returncode == 2, output
    assert "".join(reason.split()) in diagnostic, output
    assert "broken.env" in diagnostic, output
    assert "Traceback" not in output
    assert "KEY=value" not in output
