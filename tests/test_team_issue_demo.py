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
    assert team.roles["coder"].tools == ["file_read", "apply_patch", "bash"]
    assert team.roles["tester"].tools == ["file_read", "bash"]
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
    assert "--allow-local-child-tests" not in launcher
    assert "--hold" in launcher


async def _native_child_test(scheduler, role, workspace):
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.application.tool_execution import ToolRuntime

    environment = LocalEnvironment(str(workspace))
    session = scheduler._session_factory.build_spawn_session(
        role=role, env=environment, budget=10_000, aid=1, scheduler=scheduler,
        task="Run the complete fixture tests",
    )
    bash = next(tool for tool in session.agent.tools if tool.name == "bash")
    runtime = ToolRuntime(
        environment=environment, safety_policy=session.tool_execution.safety_policy,
        permission_policy=session.tool_execution.permission_policy,
    )
    try:
        import shlex

        return await bash.execute_with_runtime(
            {"command": f"{shlex.quote(sys.executable)} -m pytest -q"}, runtime,
        )
    finally:
        await session.aclose()
        await environment.cleanup()


async def test_dynamic_demo_roles_execute_fixture_after_explicit_host_shell_opt_in(tmp_path):
    from opencollab.bootstrap import build_runtime_context, build_scheduler

    workspace = tmp_path / "workspace"
    shutil.copytree(DEMO_ROOT / "workspace", workspace)
    ctx = build_runtime_context(str(workspace), {
        "model": "gpt-4o", "provider": "openai",
        "api_key": "test-key",  # pragma: allowlist secret
        "base_url": None, "budget": 100_000,
    }, trace=False)
    scheduler = build_scheduler(
        ctx, use_worktrees=False, interactive=True, auto_save=False,
        team_config_path=str(DEMO_ROOT / "team.yaml"),
        allow_unisolated_child_shell=True,
    )
    try:
        for role in ("coder", "tester"):
            failed = await _native_child_test(scheduler, role, workspace)
            assert "Exit code: 1" in failed
            assert "1 failed, 2 passed" in failed
        source = workspace / "labeler.py"
        source.write_text(source.read_text().replace(
            'value.strip().lower().replace(" ", "-")', '"-".join(value.lower().split())',
        ))
        for role in ("coder", "tester"):
            passed = await _native_child_test(scheduler, role, workspace)
            assert "Exit code: 0" in passed
            assert "3 passed" in passed
    finally:
        await scheduler.cleanup()


async def test_dynamic_demo_roles_require_isolation_without_opt_in(tmp_path):
    from opencollab.bootstrap import build_runtime_context, build_scheduler

    workspace = tmp_path / "workspace"
    shutil.copytree(DEMO_ROOT / "workspace", workspace)
    # Executing this fixture would create a marker before pytest collects tests.
    (workspace / "conftest.py").write_text(
        "from pathlib import Path\nPath('executed').touch()\n",
    )
    ctx = build_runtime_context(str(workspace), {
        "model": "gpt-4o", "provider": "openai",
        "api_key": "test-key",  # pragma: allowlist secret
        "base_url": None, "budget": 100_000,
    }, trace=False)
    scheduler = build_scheduler(
        ctx, use_worktrees=False, interactive=True, auto_save=False,
        team_config_path=str(DEMO_ROOT / "team.yaml"),
        allow_unisolated_shell=True,
    )
    try:
        for role in ("coder", "tester"):
            result = await _native_child_test(scheduler, role, workspace)
            assert "does not provide an OS process sandbox" in result
        assert not (workspace / "executed").exists()
    finally:
        await scheduler.cleanup()


def test_demo_requires_explicit_authorization_before_starting():
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "demo_team_issue.sh")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "--allow-local-child-shell" in result.stderr
    assert "host shell commands" in result.stderr
