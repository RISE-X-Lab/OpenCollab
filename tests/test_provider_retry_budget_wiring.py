from __future__ import annotations

from dataclasses import fields

import pytest

from opencollab.adapters.llm import client as client_module
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.retry import RetryTimeBudget
from opencollab.bootstrap import container
from opencollab.bootstrap._workflow_runtime_session import WorkflowSessionFactory
from opencollab.domain.agent import Agent


class _FakeLLM:
    def __init__(self, **kwargs):
        self.configuration = kwargs

    def context_window(self):
        return None

    async def close(self):
        return None


def test_llm_client_keeps_legacy_positional_context_window(monkeypatch):
    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", lambda **_kwargs: object())

    client = LLMClient(
        "model",
        "fake",
        "http://provider.invalid",
        "openai",
        "chat_completions",
        3,
        600.0,
        30.0,
        180.0,
        180.0,
        123_456,
    )

    assert client.context_window() == 123_456
    assert client.provider_error_time_budget == 0.0


def test_agent_new_retry_fields_follow_the_legacy_positional_fields():
    names = [item.name for item in fields(Agent)]

    assert names.index("tool_choice") < names.index("llm_max_retries")
    assert names.index("tool_choice") < names.index("provider_error_time_budget")


def test_workflow_sessions_and_summarizers_share_one_provider_retry_budget(
    monkeypatch,
    tmp_path,
):
    clients = []
    summarizer_budgets = []
    real_build_summarizer = container._build_summarizer

    def fake_client(**kwargs):
        client = _FakeLLM(**kwargs)
        clients.append(client)
        return client

    def capture_summarizer_budget(*args, **kwargs):
        summarizer_budgets.append(args[5])
        return real_build_summarizer(*args, **kwargs)

    monkeypatch.setattr(container, "LLMClient", fake_client)
    monkeypatch.setattr(container, "_build_summarizer", capture_summarizer_budget)
    factory = WorkflowSessionFactory(
        model="model",
        provider="openai",
        api_key="fake",  # pragma: allowlist secret
        base_url="http://provider.invalid",
        workspace=str(tmp_path),
        llm_max_retries=32,
        provider_error_time_budget=120,
    )

    factory.build_workflow_session(prompt="first", budget=100)
    factory.build_workflow_session(prompt="second", budget=100)

    client_budgets = [client.configuration["provider_retry_budget"] for client in clients]
    assert len(client_budgets) == 2
    assert client_budgets[0] is client_budgets[1]
    assert summarizer_budgets == client_budgets
    assert clients[0].configuration["max_retries"] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "responses", "anthropic"])
async def test_all_provider_paths_receive_the_shared_retry_budget(monkeypatch, mode):
    captured = []

    async def fake_complete(*_args, **kwargs):
        captured.append(kwargs["provider_error_time_budget"])
        return object()

    async def skip_usage(**_kwargs):
        return None

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(client_module, "complete_openai", fake_complete)
    monkeypatch.setattr(client_module, "complete_responses", fake_complete)
    monkeypatch.setattr(client_module, "complete_anthropic", fake_complete)
    monkeypatch.setattr(client_module, "_record_api_usage_async", skip_usage)
    budget = RetryTimeBudget(120)
    client = LLMClient(
        model="model",
        api_key="fake",  # pragma: allowlist secret
        wire_protocol="responses" if mode == "responses" else "chat_completions",
        provider_error_time_budget=120,
        provider_retry_budget=budget,
    )
    if mode == "anthropic":
        client._anthropic = object()
        client._openai = None

    await client.complete([{"role": "user", "content": "hello"}])

    assert captured == [budget]
