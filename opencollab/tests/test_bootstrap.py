import os

import pytest

from opencollab.bootstrap import (
    build_chat_session,
    build_team,
    build_runtime_context,
)
from opencollab.tools.safety import SandboxInterceptor


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


def test_build_chat_session_uses_repo_map_and_tools(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi")

    ctx = build_runtime_context(
        str(workspace),
        _cfg(),
        trace=False,
    )
    session = build_chat_session(ctx)

    system_message = session.messages[0]
    assert system_message["role"] == "system"
    assert "Project Structure:" in system_message["content"]

    tool_names = {t.name for t in session.agent.tools}
    assert tool_names == {"bash", "file_read", "file_write", "grep", "ask_user"}


def test_build_chat_session_wires_workspace_safety_policy(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(
        str(workspace),
        _cfg(),
        trace=False,
    )
    session = build_chat_session(ctx, auto_save=False)

    policy = session.tool_processor.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(workspace.resolve())
    assert policy.check_path("inside.txt").startswith(str(workspace.resolve()))
    with pytest.raises(PermissionError):
        policy.check_path("/etc/passwd")


def test_build_team_wires_lead_safety_policy_from_bootstrap(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(
        str(workspace),
        _cfg(),
        trace=False,
    )
    team = build_team(ctx, use_worktrees=False, interactive=False)

    policy = team.lead_session.tool_processor.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(workspace.resolve())


def test_build_chat_session_wires_event_sink_and_tracer_from_context(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    seen: list = []

    async def sink(event):
        seen.append(event)

    ctx = build_runtime_context(
        str(workspace),
        _cfg(),
        trace=False,
        event_sink=sink,
    )
    session = build_chat_session(ctx)

    assert session.tracer is ctx.tracer  # propagated (None when trace=False)
    # The injected sink must be one of the bus subscribers.
    assert sink in list(session.event_bus._targets)


def test_build_chat_session_auto_save_path_lands_under_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
    session = build_chat_session(ctx)

    assert session.auto_save_path is not None
    assert session.auto_save_path.startswith(
        os.path.join(str(workspace.resolve()), ".opencollab", "sessions")
    )
    assert os.path.exists(session.auto_save_path)


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
