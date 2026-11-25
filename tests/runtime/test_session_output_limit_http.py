"""Session output limits reach real SDK requests, including the default value."""

from __future__ import annotations

import asyncio
import json
from functools import wraps

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application._session_run_shared import _TokenBudgetStop
from opencollab.bootstrap import container
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.fixture(autouse=True)
def isolated_request_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")

@pytest.fixture(params=[
    ("openai", "chat_completions", "gpt-4o", "max_tokens"),
    ("openai", "chat_completions", "gpt-5", "max_completion_tokens"),
    ("openai", "responses", "gpt-4o", "max_output_tokens"),
    ("anthropic", "chat_completions", "claude-sonnet-4-6", "max_tokens"),
])
def sdk_configuration(request):
    provider, protocol, model, field = request.param
    return {"provider": provider, "wire_protocol": protocol, "model": model}, field

@pytest.mark.parametrize("configured_limit", [None, 8191, 8192, 8193])
async def test_public_agent_sends_default_and_explicit_output_limits(
    monkeypatch, tmp_path, sdk_configuration, configured_limit,
):
    configuration, field = sdk_configuration
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        output_tokens = min(9000, body[field]) if field in body else 9000
        return completion_http_response(request, output_tokens=output_tokens)

    install_sdk_transport(monkeypatch, configuration["provider"], handler)
    app = OpenCollab(
        tmp_path, model=configuration["model"], provider=configuration["provider"],
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": configuration["wire_protocol"],
                **({"max_output_tokens": configured_limit} if configured_limit else {})},
    )
    result = await app.agent("Return ok", tools=(), budget=1_000_000, max_steps=2, trace=False)

    assert result.status == "completed"
    assert len(requests) == 1
    assert requests[0][field] == (8192 if configured_limit is None else configured_limit)
    assert result.tokens == 2 + requests[0][field]

async def test_finite_budget_reduces_the_default_limit_after_reserving_input(
    monkeypatch, tmp_path, sdk_configuration, local_environment_factory,
):
    configuration, field = sdk_configuration
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, configuration["provider"], handler)
    async with LLMClient(
        **configuration, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        messages = [{"role": "user", "content": "Return ok"}]
        budget = client.estimate_request_tokens(messages) + 3000
        session = build_session(
            agent=Agent(name="budgeted", system_prompt="system", **configuration),
            llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=budget,
        )
        session.messages = messages
        assert await session.run_loop() == "ok"

    assert len(requests) == 1
    assert requests[0][field] == 3000

@pytest.mark.parametrize("injected", [False, True])
async def test_summary_sends_the_default_limit_through_the_provider_sdk(
    monkeypatch, tmp_path, sdk_configuration, injected, local_environment_factory,
):
    configuration, field = sdk_configuration
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request, text="<summary>Preserved context.</summary>")

    install_sdk_transport(monkeypatch, configuration["provider"], handler)
    async with LLMClient(
        **configuration, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        agent = Agent(
            name="summary", system_prompt="system", **configuration,
            api_key="controlled-test",  # pragma: allowlist secret
            base_url="https://controlled.invalid/v1",
        )
        session = build_session(
            agent=agent, llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=None,
        )
        summarizer = container._build_summarizer(
            agent, client if injected else None, client, 600.0, None,
            completion_handler=session.runner._invoke_summary,
        )
        assert await summarizer.asummarize([{"role": "user", "content": "Keep this context"}]) == (
            "Summary:\nPreserved context."
        )
        assert session.used_tokens == 4

    assert len(requests) == 1
    assert requests[0][field] == 8192

@pytest.mark.parametrize("configured_limit", [8192, 7000])
@pytest.mark.parametrize("client_signature", ["legacy", "explicit", "kwargs"])
async def test_injected_client_keeps_legacy_defaults_and_receives_supported_limits(
    tmp_path, configured_limit, client_signature, local_environment_factory,
):
    calls = []

    class LegacyClient:
        async def complete(self, messages, tools=None, temperature=0.0):
            calls.append({})
            return LLMResponse(content="ok", finish_reason="stop", usage=Usage())

    class ExplicitClient:
        async def complete(self, messages, tools=None, temperature=0.0, max_output_tokens=None):
            calls.append({"max_output_tokens": max_output_tokens})
            return LLMResponse(content="ok", finish_reason="stop", usage=Usage())

    class KeywordClient:
        async def complete(self, messages, tools=None, **kwargs):
            calls.append(kwargs)
            return LLMResponse(content="ok", finish_reason="stop", usage=Usage())

    client = {"legacy": LegacyClient, "explicit": ExplicitClient, "kwargs": KeywordClient}[client_signature]()
    session = build_session(
        agent=Agent(name="injected", system_prompt="system", max_tokens_per_step=configured_limit),
        llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=None,
    )
    session.messages = [{"role": "user", "content": "Return ok"}]
    if client_signature == "legacy" and configured_limit != 8192:
        with pytest.raises(TypeError, match="max_output_tokens"):
            await session.run_loop()
        assert calls == []
    else:
        assert await session.run_loop() == "ok"
        assert len(calls) == 1
        assert calls[0].get("max_output_tokens") == (None if client_signature == "legacy" else configured_limit)

async def test_legacy_injected_client_summary_retains_the_original_signature(tmp_path, local_environment_factory):
    class LegacyClient:
        async def complete(self, messages, tools=None, temperature=0.0):
            return LLMResponse(content="<summary>Preserved context.</summary>", finish_reason="stop", usage=Usage())

    client = LegacyClient()
    agent = Agent(name="legacy", system_prompt="system")
    session = build_session(
        agent=agent, llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=None,
    )
    summarizer = container._build_summarizer(
        agent, client, client, 600.0, None, completion_handler=session.runner._invoke_summary,
    )
    assert await summarizer.asummarize([{"role": "user", "content": "Keep this context"}]) == (
        "Summary:\nPreserved context."
    )


class _LegacyNativeClient(LLMClient):
    @wraps(LLMClient.complete)
    async def complete(self, messages, tools=None, temperature=0.0, *, max_output_tokens=None, on_admitted=None):
        self.received_admission_callback = on_admitted is not None
        return await super().complete(
            messages, tools=tools, temperature=temperature,
            max_output_tokens=max_output_tokens, on_admitted=on_admitted,
        )


class _SettlementWrapper(_LegacyNativeClient):
    @wraps(_LegacyNativeClient.complete)
    async def complete(self, *args, on_usage=None, **kwargs):
        self.received_usage_callback = on_usage is not None
        response = await super().complete(*args, **kwargs)
        if on_usage is not None:
            on_usage(response.usage)
        return response


class _RevalidatingWrapper(_LegacyNativeClient):
    wait_for_prompt = None

    @wraps(_LegacyNativeClient.complete)
    async def complete(self, *args, **kwargs):
        messages = args[0] if args else kwargs["messages"]
        if self.wait_for_prompt is None or messages[-1]["content"] == self.wait_for_prompt:
            self.arrived.set()
            await self.release.wait()
        before_request = kwargs.pop("before_request", None)
        if before_request is not None:
            kwargs["max_output_tokens"] = min(kwargs["max_output_tokens"], before_request())
        return await super().complete(*args, **kwargs)


class _ClassOptInWrapper(_RevalidatingWrapper):
    supports_request_revalidation = True


class _SlotOptInWrapper(_RevalidatingWrapper):
    __slots__ = ("supports_request_revalidation",)


class _PropertyOptInWrapper(_RevalidatingWrapper):
    @property
    def supports_request_revalidation(self):
        return True


@pytest.mark.parametrize("purpose", ["completion", "summary"])
@pytest.mark.parametrize(
    "client_class",
    [_RevalidatingWrapper, _ClassOptInWrapper, _SlotOptInWrapper, _PropertyOptInWrapper],
    ids=["instance", "class", "slot", "property"],
)
async def test_decorated_client_revalidates_budget_before_sending(
    tmp_path, monkeypatch, local_environment_factory, purpose, client_class,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request, text="<summary>Preserved context.</summary>")

    install_sdk_transport(monkeypatch, "openai", handler)
    async with client_class(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        if client_class in {_RevalidatingWrapper, _SlotOptInWrapper}:
            client.supports_request_revalidation = True
        client.arrived, client.release = asyncio.Event(), asyncio.Event()
        session = build_session(
            agent=Agent(name="queued", system_prompt="system", max_tokens_per_step=128),
            llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=20_000,
        )
        await session.add_user_message("Keep this context")
        operation = (
            session._context_summarizer.asummarize(session.messages)
            if purpose == "summary" else session.run_loop()
        )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(client.arrived.wait(), 5)
            session.max_budget_tokens = 1
            client.release.set()
            if purpose == "summary":
                with pytest.raises(_TokenBudgetStop):
                    await asyncio.wait_for(task, 5)
            else:
                assert await asyncio.wait_for(task, 5) == ""
                assert "budget exhausted before model call" in session.state.terminal_reason
            assert requests == []
            assert session.used_tokens == 0
        finally:
            client.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await session.aclose()


async def test_decorated_slot_client_revalidates_shared_workflow_spend(tmp_path, monkeypatch):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        requests.append((prompt, body["max_tokens"]))
        response = completion_http_response(request)
        if prompt == "spender":
            usage = {"prompt_tokens": 1997, "completion_tokens": 2, "total_tokens": 1999}
            return http_response(request, 200, json=response.json() | {"usage": usage})
        return response

    install_sdk_transport(monkeypatch, "openai", handler)
    async with _SlotOptInWrapper(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        client.supports_request_revalidation = True
        client.wait_for_prompt = "waiter"
        client.arrived, client.release = asyncio.Event(), asyncio.Event()

        async def flow(ctx, _inputs):
            waiter = asyncio.create_task(ctx.agent_run("waiter", tools=[], system_prompt="System.", budget=1000))
            try:
                await asyncio.wait_for(client.arrived.wait(), 5)
                spender = await ctx.agent_run("spender", tools=[], system_prompt="System.", budget=1000)
                assert spender.tokens == ctx.tokens_spent() == 1999
                client.release.set()
                return await asyncio.wait_for(waiter, 5)
            finally:
                client.release.set()
                await asyncio.gather(waiter, return_exceptions=True)

        result = await OpenCollab(tmp_path, config={"max_output_tokens": 128}).workflow(
            flow, llm=client, budget=2000, trace=False, limit_mode="explicit", concurrency=2,
        )

    assert result.status == "completed"
    assert result.output.status == "stopped"
    assert "budget exhausted before model call" in result.output.reason
    assert result.output.tokens == 0
    assert result.tokens == 1999
    assert requests == [("spender", 128)]


class _ForwardingWrapper(_LegacyNativeClient):
    @wraps(_LegacyNativeClient.complete)
    async def complete(self, *args, **kwargs):
        return await super().complete(*args, **kwargs)


class _ForwardingProxy:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    @wraps(_ForwardingWrapper.complete)
    async def complete(self, *args, **kwargs):
        return await self.client.complete(*args, **kwargs)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("client_class", [_LegacyNativeClient, _SettlementWrapper, _ForwardingWrapper])
async def test_public_agent_charges_legacy_native_wrapper_usage_once(tmp_path, monkeypatch, failed, client_class):
    requests = []
    events = []

    async def handler(request):
        requests.append(json.loads(request.content))
        response = completion_http_response(request)
        if failed:
            return http_response(request, 200, json=response.json() | {"choices": []})
        return response

    install_sdk_transport(monkeypatch, "openai", handler)
    pool = tmp_path / "pool"
    pool.mkdir()
    async with client_class(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
        request_pool=str(pool), request_capacity=1,
    ) as client:
        llm = _ForwardingProxy(client) if client_class is _ForwardingWrapper else client
        result = await OpenCollab(tmp_path).agent(
            "Return ok", llm=llm, tools=(), budget=20_000, trace=False,
            run_control=RunControl(on_event=events.append),
        )

    assert client.received_admission_callback is True
    if client_class is _SettlementWrapper:
        assert client.received_usage_callback is True
    assert len(requests) == 1 and requests[0]["max_tokens"] == 8192
    assert result.status == ("failed" if failed else "completed")
    assert result.tokens == 4
    usage = [event.data for event in events if event.type == "usage"]
    assert [event["total_tokens"] for event in usage] == [4]
    assert usage[0]["error_type"] == ("IndexError" if failed else None)


async def test_summary_charges_legacy_native_wrapper_usage_once(tmp_path, monkeypatch, local_environment_factory):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request, text="<summary>Preserved context.</summary>")

    install_sdk_transport(monkeypatch, "openai", handler)
    async with _LegacyNativeClient(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        agent = Agent(name="legacy", system_prompt="system")
        session = build_session(
            agent=agent, llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=None,
        )
        summarizer = container._build_summarizer(
            agent, client, client, 600.0, None, completion_handler=session.runner._invoke_summary,
        )
        try:
            assert await summarizer.asummarize([{"role": "user", "content": "Keep this context"}]) == (
                "Summary:\nPreserved context."
            )
            assert session.used_tokens == 4
        finally:
            await session.aclose()

    assert len(requests) == 1 and requests[0]["max_tokens"] == 8192
