from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest

from opencollab.adapters.llm import client as transport
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.request_admission import request_slot
from opencollab.adapters.llm.types import LLMResponse
from tests.support.paths import PACKAGE_ROOT


@pytest.mark.parametrize("configuration, provider_function", [
    ({"model": "gpt-4o"}, "complete_openai"),
    ({"model": "gpt-4o", "wire_protocol": "responses"}, "complete_responses"),
    ({"model": "claude-sonnet-4-6", "provider": "anthropic"}, "complete_anthropic"),
], ids=["chat", "responses", "anthropic"])
@pytest.mark.parametrize("consume_hooks", [False, True])
@pytest.mark.parametrize("configured_limit", [None, 50, 128])
async def test_slot_admission_prepares_first_request_before_provider_entry(
    tmp_path, monkeypatch, configuration, provider_function, consume_hooks, configured_limit,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    queued = asyncio.Event()
    events = []
    limits = iter([80, 31])
    native_slot = transport.request_slot
    first_limit = 80 if configured_limit is None else min(configured_limit, 80)

    @asynccontextmanager
    async def observed_slot(*args):
        queued.set()
        async with native_slot(*args):
            yield

    def revalidate():
        limit = next(limits)
        events.append(f"limit_{limit}")
        return limit

    async def complete(*_args, **kwargs):
        events.append("provider")
        assert events == ["limit_80", "admitted", "provider"]
        assert kwargs["max_output_tokens"] == first_limit
        if consume_hooks:
            assert kwargs["before_attempt"]() == first_limit
            assert events == ["limit_80", "admitted", "provider"]
            assert kwargs["before_attempt"]() == 31
        return LLMResponse(content="answer")

    monkeypatch.setattr(transport, "request_slot", observed_slot)
    monkeypatch.setattr(transport, provider_function, complete)
    async with LLMClient(
        **configuration, api_key="local-test", request_pool=str(tmp_path), request_capacity=1,
    ) as client:
        async with native_slot(str(tmp_path), 1):
            owner = asyncio.create_task(client.complete(
                [], max_output_tokens=configured_limit, before_request=revalidate,
                on_admitted=lambda: events.append("admitted"),
            ))
            await asyncio.wait_for(queued.wait(), 1)
            assert events == []
        assert (await asyncio.wait_for(owner, 1)).content == "answer"
    assert events == ["limit_80", "admitted", "provider", *(["limit_31"] if consume_hooks else [])]


async def test_cancellation_retains_slot_until_native_cleanup_finishes(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise
        return LLMResponse(content="finished")

    monkeypatch.setattr(transport, "complete_openai", complete)
    kwargs = {"api_key": "local-test", "request_pool": str(tmp_path), "request_capacity": 1}
    async with LLMClient(**kwargs) as first, LLMClient(**kwargs) as second:
        active = asyncio.create_task(first.complete([]))
        await asyncio.wait_for(entered.wait(), 2)
        active.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        queued = asyncio.create_task(second.complete([]))
        await asyncio.sleep(0.12)
        assert calls == [0]
        assert not queued.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert (await asyncio.wait_for(queued, 2)).content == "finished"
    assert calls == [0, 1]


async def test_cancelled_waiter_never_releases_someone_elses_slot(tmp_path):
    async with request_slot(str(tmp_path), 1):
        async def wait():
            async with request_slot(str(tmp_path), 1):
                pytest.fail("a cancelled waiter entered an occupied pool")

        queued = asyncio.create_task(wait())
        await asyncio.sleep(0.1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        following = asyncio.create_task(wait())
        await asyncio.sleep(0.1)
        assert not following.done()
        following.cancel()
        with pytest.raises(asyncio.CancelledError):
            await following
    async with request_slot(str(tmp_path), 1):
        pass


async def test_native_failure_keeps_slot_until_provider_returns(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def complete(*_args, **_kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise ValueError("terminal provider failure")
        return LLMResponse(content="next")

    monkeypatch.setattr(transport, "complete_openai", complete)
    kwargs = {"api_key": "local-test", "request_pool": str(tmp_path), "request_capacity": 1}
    async with LLMClient(**kwargs) as first, LLMClient(**kwargs) as second:
        active = asyncio.create_task(first.complete([]))
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.create_task(second.complete([]))
        await asyncio.sleep(0.1)
        assert calls == [0]
        release.set()
        with pytest.raises(ValueError, match="terminal provider failure"):
            await active
        assert (await asyncio.wait_for(queued, 2)).content == "next"


async def test_pool_rejects_capacity_disagreement_and_missing_mount(tmp_path):
    async with request_slot(str(tmp_path), 2):
        with pytest.raises(ValueError, match="already has capacity 2"):
            async with request_slot(str(tmp_path), 3):
                pass
    with pytest.raises(ValueError, match="Mount the same directory"):
        async with request_slot(str(tmp_path / "missing-mount"), 1):
            pass
    assert not (tmp_path / "missing-mount").exists()


async def test_process_death_releases_os_slot(tmp_path):
    script = """
import asyncio, sys
from opencollab.adapters.llm.request_admission import request_slot
async def main():
    async with request_slot(sys.argv[1], 1):
        print('entered', flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)}
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10) == "entered\n"
        process.kill()
        await asyncio.wait_for(asyncio.to_thread(process.wait), 5)

        async def acquire():
            async with request_slot(str(tmp_path), 1):
                return "recovered"

        assert await asyncio.wait_for(acquire(), 2) == "recovered"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


async def test_native_retry_stays_inside_the_same_request_slot(tmp_path, monkeypatch):
    from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport

    failed = asyncio.Event()
    retrying = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(request):
        import json

        model = json.loads(request.content)["model"]
        calls.append(model)
        if len(calls) == 1:
            failed.set()
            return http_response(request, 503, json={"error": {"message": "temporarily unavailable"}})
        if model == "first":
            retrying.set()
            await release.wait()
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "openai", handler)
    kwargs = {"api_key": "local", "request_pool": str(tmp_path), "request_capacity": 1, "max_retries": 1}
    async with LLMClient(model="first", **kwargs) as first, LLMClient(model="second", **kwargs) as second:
        active = asyncio.create_task(first.complete([]))
        await asyncio.wait_for(failed.wait(), 2)
        queued = asyncio.create_task(second.complete([]))
        await asyncio.wait_for(retrying.wait(), 3)
        assert calls == ["first", "first"]
        assert not queued.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(active, queued), 3)
    assert calls == ["first", "first", "second"]
