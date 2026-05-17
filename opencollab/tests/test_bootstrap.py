import os

import pytest

from opencollab.bootstrap import (
    build_chat_session,
    build_runtime_context,
)


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
        yolo=True,
    )
    session = build_chat_session(ctx)

    system_message = session.messages[0]
    assert system_message["role"] == "system"
    assert "Project Structure:" in system_message["content"]

    tool_names = {t.name for t in session.agent.tools}
    assert tool_names == {"bash", "file_read", "file_write", "grep", "ask_user"}


def test_build_runtime_context_resolves_workspace_and_tracer(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    monkeypatch.chdir(tmp_path)

    ctx_no_trace = build_runtime_context(
        "ws", _cfg(), trace=False, yolo=True,
    )
    assert ctx_no_trace.tracer is None
    assert os.path.isabs(ctx_no_trace.workspace)
    assert ctx_no_trace.workspace == str(workspace.resolve())

    ctx_trace = build_runtime_context(
        "ws", _cfg(), trace=True, yolo=True, run_id_prefix="bootstrap-",
    )
    try:
        assert ctx_trace.tracer is not None
        assert os.path.exists(ctx_trace.tracer.path)
        assert os.path.basename(ctx_trace.tracer.path).startswith("bootstrap-")
    finally:
        if ctx_trace.tracer:
            ctx_trace.tracer.close()
