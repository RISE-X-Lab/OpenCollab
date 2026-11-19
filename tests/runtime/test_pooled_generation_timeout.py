from __future__ import annotations

import asyncio
import json
import threading
from functools import wraps

import pytest

from opencollab.adapters.llm import client as transport
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.request_admission import request_slot
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.run_control import RunControl
from opencollab.application.session_run import GenerationTimeoutError
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from tests.runtime.test_async_compaction import history
from tests.support.environment_test_support import local_environment_factory as local_environment_factory


class _LegacyPooledClient(LLMClient):
    @wraps(LLMClient.complete)
    async def complete(self, messages, tools=None, temperature=0.0, *, max_output_tokens=None):
        return await super().complete(messages, tools, temperature, max_output_tokens=max_output_tokens)


class _ForwardingPooledClient(_LegacyPooledClient):
    @wraps(_LegacyPooledClient.complete)
    async def complete(self, *args, **kwargs):
        return await super().complete(*args, **kwargs)


class _DelegatingPooledClient(_LegacyPooledClient):
    @wraps(_LegacyPooledClient.complete)
    async def complete(self, *args, **kwargs):
        return await self.delegate(*args, **kwargs)


class _AdmissionMiddleware(_LegacyPooledClient):
    supports_request_admission = True

    @wraps(_LegacyPooledClient.complete)
    async def complete(self, *args, **kwargs):
        on_admitted = kwargs.pop("on_admitted", None)
        return await LLMClient.complete(self, *args, on_admitted=on_admitted, **kwargs)


class _PreparingPooledClient(_LegacyPooledClient):
    @wraps(_LegacyPooledClient.complete)
    async def complete(self, *args, **kwargs):
        await asyncio.sleep(0.15)
        return await LLMClient.complete(self, *args, **kwargs)


class _AdmissionProxy:
    __slots__ = ("client",)

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)


@pytest.fixture(
    params=[LLMClient, _AdmissionMiddleware, _ForwardingPooledClient],
    ids=["native", "decorated", "inherited"],
)
def pooled_client_class(request):
    return request.param


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("proxy", [False, True], ids=["direct-client", "attribute-proxy"])
async def test_queue_wait_leaves_the_full_provider_timeout(
    tmp_path, monkeypatch, local_environment_factory, summary, proxy, pooled_client_class,
):
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append("started")
        await asyncio.sleep(0.06)
        return LLMResponse(content="<summary>Earlier context.</summary>" if summary and len(calls) == 1 else "answer")

    monkeypatch.setattr(transport, "complete_openai", complete)
    async with pooled_client_class(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="queued", system_prompt="system"), llm=_AdmissionProxy(client) if proxy else client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.1,
        )
        session.messages = history(summary)
        async with request_slot(str(tmp_path), 1):
            owner = asyncio.create_task(session.run_loop())
            await asyncio.sleep(0.18)
            assert calls == []
            assert not owner.done()
        assert await asyncio.wait_for(owner, 2) == "answer"
        assert len(calls) == (2 if summary else 1)
        await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
async def test_admitted_timeout_retains_slot_and_late_usage_until_cleanup(
    tmp_path, monkeypatch, local_environment_factory, summary, pooled_client_class,
):
    cancelled = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append("started")
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return LLMResponse(content="<summary>Earlier context.</summary>", usage=Usage(13, 24))

    monkeypatch.setattr(transport, "complete_openai", complete)
    async with pooled_client_class(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="slow", system_prompt="system"), llm=client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.08,
        )
        session.messages = history(summary)
        async with request_slot(str(tmp_path), 1):
            owner = asyncio.create_task(session.run_loop())
            await asyncio.sleep(0.15)
            assert not owner.done()
            assert calls == []
        try:
            with pytest.raises(GenerationTimeoutError):
                await asyncio.wait_for(owner, 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            assert session.pending_cleanup_tasks
            following = asyncio.create_task(client.complete([]))
            await asyncio.sleep(0.1)
            assert calls == ["started"]
            assert not following.done()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True), 1)
        await asyncio.wait_for(following, 1)
        assert session.used_tokens == 37
        assert session.runner.late_provider_usage == (37,)
        assert session.pending_cleanup_tasks == ()
        await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_outer_cancellation_removes_the_waiter_before_admission(
    tmp_path, monkeypatch, local_environment_factory, summary, stop, pooled_client_class,
):
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append("started")
        return LLMResponse(content="answer")

    monkeypatch.setattr(transport, "complete_openai", complete)
    async with pooled_client_class(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="cancelled", system_prompt="system"), llm=client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.08,
        )
        session.messages = history(summary)
        async with request_slot(str(tmp_path), 1):
            owner = asyncio.create_task(session.run_loop())
            if stop == "cancel":
                await asyncio.sleep(0.12)
                assert not owner.done()
                owner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(owner, 1)
            else:
                with pytest.raises(asyncio.TimeoutError) as timeout:
                    await asyncio.wait_for(owner, 0.12)
                assert type(timeout.value) is asyncio.TimeoutError
            await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True), 1)
        await asyncio.sleep(0.1)
        assert calls == []
        assert session.used_tokens == 0
        assert session.pending_cleanup_tasks == ()
        assert (await asyncio.wait_for(client.complete([]), 1)).content == "answer"
        await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("proxy", [False, True], ids=["direct-client", "attribute-proxy"])
async def test_legacy_pooled_forwarder_keeps_its_wrapped_signature(
    tmp_path, monkeypatch, local_environment_factory, summary, proxy,
):
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append(True)
        return LLMResponse(
            content="<summary>Earlier context.</summary>" if summary and len(calls) == 1 else "answer",
            usage=Usage(7, 3),
        )

    monkeypatch.setattr(transport, "complete_openai", complete)
    async with _ForwardingPooledClient(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        assert client.supports_request_admission is True
        session = build_session(
            agent=Agent(name="legacy", system_prompt="system"), llm=_AdmissionProxy(client) if proxy else client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=1,
        )
        session.messages = history(summary)
        try:
            assert await session.run_loop() == "answer"
            assert len(calls) == (2 if summary else 1)
            assert session.used_tokens == 10 * len(calls)
        finally:
            await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
async def test_inherited_admission_allows_middleware_preparation_before_pool(
    tmp_path, monkeypatch, local_environment_factory, summary,
):
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append(True)
        return LLMResponse(content="<summary>Earlier context.</summary>" if summary and len(calls) == 1 else "answer")

    monkeypatch.setattr(transport, "complete_openai", complete)
    async with _PreparingPooledClient(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="middleware", system_prompt="system"), llm=client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.08,
        )
        session.messages = history(summary)
        try:
            assert await asyncio.wait_for(session.run_loop(), 2) == "answer"
            assert len(calls) == (2 if summary else 1)
        finally:
            await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("bypass", ["returns", "hangs", "other-client", "nonpool"])
async def test_inherited_admission_waiting_preserves_completion_and_caller_deadline(
    tmp_path, monkeypatch, local_environment_factory, summary, bypass,
):
    calls = []
    provider_calls = []

    async def complete(*_args, **_kwargs):
        provider_calls.append(True)
        await asyncio.Event().wait()

    async def delegate(messages, tools=None, temperature=0.0, *, max_output_tokens=None):
        calls.append(True)
        if bypass == "returns":
            content = "<summary>Earlier context.</summary>" if summary and len(calls) == 1 else "answer"
            return LLMResponse(content=content, usage=Usage(7, 3))
        if bypass == "other-client":
            return await other.complete(messages, tools, temperature, max_output_tokens=max_output_tokens)
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "complete_openai", complete)
    config = dict(api_key="local", context_window=40_000)
    pool_config = dict(request_pool=str(tmp_path), request_capacity=1)
    client_pool = {} if bypass == "nonpool" else pool_config
    async with _DelegatingPooledClient(**config, **client_pool) as client, LLMClient(**config, **pool_config) as other:
        client.delegate = delegate
        session = build_session(
            agent=Agent(name="legacy", system_prompt="system"), llm=client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.1,
        )
        session.messages = history(summary)
        try:
            if bypass == "returns":
                assert await asyncio.wait_for(session.run_loop(), 2) == "answer"
                assert len(calls) == (2 if summary else 1)
                assert session.used_tokens == 10 * len(calls)
            else:
                expected_timeout = GenerationTimeoutError if bypass == "nonpool" else asyncio.TimeoutError
                with pytest.raises(expected_timeout) as timeout:
                    await asyncio.wait_for(session.run_loop(), 0.25)
                assert type(timeout.value) is expected_timeout
                assert calls == [True]
                assert session.used_tokens == 0
                await asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True)
            assert provider_calls == ([True] if bypass == "other-client" else [])
        finally:
            await session.aclose()


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
async def test_blocked_ledger_releases_slot_and_preserves_late_session_usage(
    tmp_path, monkeypatch, local_environment_factory, summary, provider_error,
):
    writing, release = threading.Event(), threading.Event()
    second_started = asyncio.Event()
    events, calls = [], []
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(log_path))
    original_record = transport.record_api_usage

    def delayed_record(**kwargs):
        if not writing.is_set():
            writing.set()
            assert release.wait(5)
        original_record(**kwargs)

    async def complete(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 2:
            second_started.set()
            return LLMResponse(content="next", usage=Usage(2, 2))
        usage = Usage(13, 24)
        if provider_error:
            exc = ValueError("provider disconnected")
            exc.usage = usage
            raise exc
        return LLMResponse(content="<summary>Earlier context.</summary>", usage=usage)

    monkeypatch.setattr(transport, "record_api_usage", delayed_record)
    monkeypatch.setattr(transport, "complete_openai", complete)
    async with LLMClient(
        api_key="local", request_pool=str(tmp_path), request_capacity=1, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="blocked-ledger", system_prompt="system"), llm=client,
            env=local_environment_factory(str(tmp_path)), llm_timeout=0.2,
            run_control=RunControl(on_event=lambda event: events.append(event) if event.type == "usage" else None),
        )
        session.messages = history(summary)
        owner = asyncio.create_task(session.run_loop())
        following = None
        try:
            assert await asyncio.to_thread(writing.wait, 5)
            assert session.used_tokens == 37
            following = asyncio.create_task(client.complete([]))
            await asyncio.wait_for(second_started.wait(), 2)
            with pytest.raises(GenerationTimeoutError):
                await asyncio.wait_for(owner, 2)
            assert session.pending_cleanup_tasks
            assert len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(owner, *session.pending_cleanup_tasks, return_exceptions=True)
            if following is not None:
                assert (await asyncio.wait_for(following, 2)).content == "next"
            await session.aclose()

    assert session.used_tokens == 37
    assert session.runner.late_provider_usage == (37,)
    assert not session.pending_cleanup_tasks
    assert len(events) == 1
    assert events[0].data["purpose"] == ("summary" if summary else "completion")
    assert events[0].data["total_tokens"] == 37
    assert events[0].data["late"] is True
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(row["status"], row["usage"]["total_tokens"]) for row in rows] == [
        ("error" if provider_error else "success", 37), ("success", 4),
    ]
