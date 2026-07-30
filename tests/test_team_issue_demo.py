from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = REPO_ROOT / "examples" / "team-issue"


def test_demo_team_has_three_roles_with_analyst_entry():
    team = load_team_config(path=DEMO_ROOT / "team.yaml")

    assert team.entry == "analyst"
    assert set(team.roles) == {"analyst", "coder", "tester"}
    assert team.roles["analyst"].tools == ["file_read", "spawn_agent"]
    assert team.roles["coder"].tools == ["file_read", "apply_patch", "run_tests"]
    assert team.roles["tester"].tools == ["file_read", "run_tests"]
    assert team.topology.allows("analyst", "coder")
    assert team.topology.allows("analyst", "tester")
    assert not team.topology.allows("coder", "tester")


def test_demo_fixture_starts_with_one_actionable_failure(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(DEMO_ROOT / "workspace", workspace)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "1 failed, 2 passed" in output
    assert "test_collapses_mixed_unicode_whitespace" in output


def test_demo_launcher_uses_explicit_team_shared_workspace_and_tui_hold():
    launcher = (REPO_ROOT / "scripts" / "demo_team_issue.sh").read_text(encoding="utf-8")

    assert "--team-config" in launcher
    assert "--prompt-file" in launcher
    assert "--no-worktrees" in launcher
    assert "--allow-local-child-tests" in launcher
    assert "--hold" in launcher
