"""Chat stream configuration reaches each runtime composition path."""

# ---------------------------------------------------------------------------
# Wiring: the switch has to reach the provider from a config file
# ---------------------------------------------------------------------------


def test_env_flag_reaches_the_config(monkeypatch, tmp_path):
    from opencollab.bootstrap.config import build_config

    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "true")

    assert build_config(str(tmp_path)).llm_stream_chat is True


def test_config_defaults_the_switch_off(monkeypatch, tmp_path):
    from opencollab.bootstrap.config import build_config

    monkeypatch.delenv("OPENCOLLAB_LLM_STREAM_CHAT", raising=False)

    assert build_config(str(tmp_path)).llm_stream_chat is False


def test_agent_flag_reaches_the_llm_client(monkeypatch):
    """Without this the setting would look enabled and change nothing — the
    failure mode that costs a whole batch of runs."""
    from opencollab.adapters.llm import client as client_module
    from opencollab.bootstrap import container
    from opencollab.domain.agent import Agent

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", lambda **_kwargs: object())
    agent = Agent(name="analyst", system_prompt="hi", llm_stream_chat=True)

    resolved = container._resolve_llm(agent, None, 600.0, None)

    assert resolved.stream_chat is True
    assert container._resolve_llm(
        Agent(name="analyst", system_prompt="hi"), None, 600.0, None
    ).stream_chat is False


def test_public_model_client_retains_the_configured_chat_stream_flag(monkeypatch, tmp_path):
    from opencollab import OpenCollab
    from opencollab.adapters.llm import client as client_module

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", lambda **_kwargs: object())
    client = OpenCollab(tmp_path, config={"model": "model", "provider": "openai", "llm_stream_chat": True})
    transport = client.create_model_client()
    assert transport.stream_chat is True
    assert client.configuration["llm_stream_chat"] is True


def test_team_lead_and_child_receive_the_same_chat_stream_switch(tmp_path):
    from opencollab.bootstrap import build_runtime_context, build_scheduler
    from opencollab.bootstrap.context_builder import ContextBuilder

    cfg = {"model": "model", "provider": "openai", "api_key": "unused", "base_url": None, "budget": 100_000,
           "llm_stream_chat": True}
    ctx = build_runtime_context(str(tmp_path), cfg, trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False, auto_save=False)
    assert scheduler.lead_session.agent.llm_stream_chat is True
    factory = scheduler._session_factory
    child_agent = ContextBuilder(factory._team, factory._cfg).build_agent("coder", scheduler=scheduler)
    assert child_agent.llm_stream_chat is True
