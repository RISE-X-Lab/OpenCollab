import json
import os

import pytest

from opencollab.adapters.safety import SandboxInterceptor
from opencollab.bootstrap import (
    build_runtime_context,
    build_scheduler,
)
from opencollab.bootstrap.team_config import DEFAULT_LEAD_PROMPT, LEAD_TOOL_NAMES
from opencollab.domain.identity import role_storage_slug


def _cfg(**overrides):
    base = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "test-key",
        "base_url": None,
        "budget": 100_000,
    }
    base.update(overrides)
    return base


def test_lead_prompt_references_spawn_tools_not_delegate():
    # Guard against the prompt/tool mismatch: agent 0 is given spawn_agent /
    # spawn_with_review, so its prompt must name those, not the removed
    # delegate_task / delegate_with_review.
    assert "spawn_agent" in DEFAULT_LEAD_PROMPT
    assert "spawn_with_review" in DEFAULT_LEAD_PROMPT
    assert "delegate_task" not in DEFAULT_LEAD_PROMPT
    assert "delegate_with_review" not in DEFAULT_LEAD_PROMPT


def test_build_scheduler_lead_has_spawn_tools(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi")
    monkeypatch.chdir(tmp_path)

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=True)
    lead = scheduler.lead_session

    assert lead.tracer is ctx.tracer  # propagated (None when trace=False)
    system_message = lead.messages[0]
    assert system_message["role"] == "system"

    # The no-team default lead gets every registered tool (interactive ⇒
    # ask_user kept); the set is derived from the registry, so assert against
    # the source constant rather than a frozen literal that would drift.
    tool_names = {t.name for t in lead.agent.tools}
    assert tool_names == set(LEAD_TOOL_NAMES)
    assert {"spawn_agent", "spawn_with_review"} <= tool_names


def test_build_scheduler_lead_omits_ask_user_when_headless(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False)

    tool_names = {t.name for t in scheduler.lead_session.agent.tools}
    assert "ask_user" not in tool_names
    assert {"spawn_agent", "spawn_with_review"} <= tool_names


def test_build_scheduler_rejects_missing_explicit_session(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)

    with pytest.raises(ValueError, match="session file does not exist"):
        build_scheduler(
            ctx,
            use_worktrees=False,
            interactive=False,
            session_file=str(tmp_path / "missing-session.json"),
        )

    assert not (workspace / ".opencollab").exists()


def test_build_scheduler_preserves_explicit_empty_thinking_params(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(
        str(workspace),
        _cfg(thinking=True, thinking_params={}),
        trace=False,
    )

    scheduler = build_scheduler(
        ctx,
        use_worktrees=False,
        interactive=False,
        auto_save=False,
    )

    assert scheduler.lead_session.agent.thinking_params == {}


def test_build_scheduler_wires_lead_safety_policy_from_bootstrap(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False)

    policy = scheduler.lead_session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(workspace.resolve())
    assert policy.check_path("inside.txt").startswith(str(workspace.resolve()))
    with pytest.raises(PermissionError):
        policy.check_path("/etc/passwd")


def test_build_scheduler_lead_auto_save_path_lands_under_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False)
    lead = scheduler.lead_session

    assert lead.auto_save_path is not None
    assert lead.auto_save_path.startswith(
        os.path.join(str(workspace.resolve()), ".opencollab", "sessions")
    )
    assert os.path.exists(lead.auto_save_path)


def test_build_scheduler_writes_structured_lead_file_and_manifest(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False)
    lead_path = scheduler.lead_session.auto_save_path

    # Lead transcript: structured JSON with metadata + per-message timestamps.
    assert os.path.basename(lead_path) == (
        f"agent_0_{role_storage_slug('lead')}.json"
    )
    with open(lead_path) as f:
        saved = json.load(f)
    assert saved["aid"] == 0
    assert saved["role"] == "lead"
    assert saved["messages"] and all("timestamp" in m for m in saved["messages"])

    # team.json manifest sits in the same run folder and lists agent 0.
    run_dir = os.path.dirname(lead_path)
    manifest_path = os.path.join(run_dir, "team.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    agents = {a["aid"]: a for a in manifest["agents"]}
    assert agents[0]["role"] == "lead"
    assert agents[0]["parent_aid"] is None
    assert "started_at" in manifest and "run_id" in manifest


def test_build_scheduler_accepts_explicit_team_file_and_run_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    team_file = tmp_path / "custom-team.yaml"
    team_file.write_text(
        """
roles:
  captain:
    prompt: Lead this run.
    tools: []
entry: captain
topology: {}
""".strip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / "artifacts"

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(
        ctx,
        use_worktrees=False,
        interactive=False,
        team_config_path=team_file,
        save_dir=run_dir,
    )

    assert scheduler.lead_session.agent.name == "captain"
    assert os.path.dirname(scheduler.lead_session.auto_save_path) == str(run_dir)
    with open(run_dir / "team.json") as handle:
        manifest = json.load(handle)
    assert manifest["team_file"] == str(team_file.resolve())


def test_build_scheduler_can_explicitly_allow_unisolated_child_tests(tmp_path):
    from opencollab.adapters._env_local import LocalEnvironment

    workspace = tmp_path / "ws"
    workspace.mkdir()
    team_file = tmp_path / "team.yaml"
    team_file.write_text(
        """
entry: analyst
roles:
  analyst:
    prompt: Analyze.
    tools: [spawn_agent]
  tester:
    prompt: Test.
    tools: [run_tests]
topology:
  analyst: [tester]
  tester: []
""".strip(),
        encoding="utf-8",
    )
    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    scheduler = build_scheduler(
        ctx,
        use_worktrees=False,
        interactive=True,
        auto_save=False,
        team_config_path=team_file,
        allow_unisolated_child_tests=True,
    )

    child = scheduler._session_factory.build_spawn_session(
        role="tester",
        env=LocalEnvironment(str(workspace)),
        budget=1_000,
    )
    run_tests = child.agent.find_tool("run_tests")

    assert run_tests.require_process_isolation is False
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_build_runtime_context_resolves_workspace_and_tracer(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    monkeypatch.chdir(tmp_path)

    ctx_no_trace = build_runtime_context(
        "ws", _cfg(), trace=False,
    )
    assert ctx_no_trace.tracer is None
    assert os.path.isabs(ctx_no_trace.workspace)
    assert ctx_no_trace.workspace == str(workspace.resolve())

    ctx_trace = build_runtime_context(
        "ws", _cfg(), trace=True, run_id_prefix="bootstrap-",
    )
    try:
        assert ctx_trace.tracer is not None
        assert os.path.exists(ctx_trace.tracer.path)
        assert os.path.basename(ctx_trace.tracer.path).startswith("bootstrap-")
    finally:
        if ctx_trace.tracer:
            ctx_trace.tracer.close()
