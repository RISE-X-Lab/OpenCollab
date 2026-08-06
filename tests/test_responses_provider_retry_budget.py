from __future__ import annotations

from types import SimpleNamespace as ns

import pytest
from responses_provider_test_support import (
    FakeStream,
    completed_response,
    message_item,
)

from opencollab.adapters.llm.errors import TransientProviderError
from opencollab.adapters.llm.responses_provider import complete_responses
from opencollab.adapters.llm.retry import RetryTimeBudget


@pytest.mark.asyncio
async def test_provider_error_budget_uses_configured_retry_count(monkeypatch):
    calls = 0

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 5:
                raise RuntimeError("server disconnected")
            item = message_item("recovered")
            return FakeStream([
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.completed", response=completed_response(output=[item])),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "wait"}],
        None,
        0,
        4,
        first_event_timeout=1,
        stream_idle_timeout=1,
        round_timeout=2,
        provider_error_time_budget=RetryTimeBudget(30),
    )

    assert calls == 5
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_provider_error_budget_does_not_bypass_retry_count(monkeypatch):
    calls = 0

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("server disconnected")

    with pytest.raises(RuntimeError, match="server disconnected"):
        await complete_responses(
            ns(responses=Responses()),
            "gpt-fake",
            [{"role": "user", "content": "wait"}],
            None,
            0,
            1,
            first_event_timeout=1,
            stream_idle_timeout=1,
            round_timeout=2,
            provider_error_time_budget=RetryTimeBudget(30),
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_budget_mode_keeps_normal_round_timeout_and_closes_stream():
    stream = FakeStream([ns(type="response.created")], delays=[10])

    class Responses:
        async def create(self, **_kwargs):
            return stream

    with pytest.raises(TransientProviderError, match="request timeout"):
        await complete_responses(
            ns(responses=Responses()),
            "gpt-fake",
            [{"role": "user", "content": "wait"}],
            None,
            0,
            0,
            first_event_timeout=1,
            stream_idle_timeout=1,
            round_timeout=0.001,
            provider_error_time_budget=RetryTimeBudget(30),
        )

    assert stream.closed is True
