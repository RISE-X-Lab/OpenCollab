from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import client as transport
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.request_admission import request_slot
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.application._session_run_shared import _TokenBudgetStop
from opencollab.bootstrap.authored_workflow_bridge import AuthoredWorkflowBridge
from tests.runtime.test_llm_chat_error_usage_http import ReportedUsageStream
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.fixture(params=[
    ({"provider": "openai", "wire_protocol": "chat_completions", "model": "gpt-4o"}, "max_tokens"),
    ({"provider": "openai", "wire_protocol": "responses", "model": "gpt-4o"}, "max_output_tokens"),
    ({"provider": "anthropic", "model": "claude-sonnet-4-6"}, "max_tokens"),
], ids=["chat", "responses", "anthropic"])
def configuration(request):
    return request.param


@pytest.mark.parametrize("outcome", ["success", "ledger_error", "ledger_cancellation"])
async def test_native_usage_is_reported_once_inside_the_slot(
    tmp_path, monkeypatch, configuration, outcome,
):
    config, _field = configuration
    usages = []
    install_sdk_transport(monkeypatch, config["provider"], completion_http_response)
    native_slot = transport.request_slot

    @asynccontextmanager
    async def observed_slot(*args):
        async with native_slot(*args):
            try:
                yield
            finally:
                assert [usage.total_tokens for usage in usages] == [4]

    async def record(**kwargs):
        if outcome == "ledger_error":
            raise ValueError("controlled ledger failure")
        if outcome == "ledger_cancellation":
            raise asyncio.CancelledError

    monkeypatch.setattr(transport, "request_slot", observed_slot)
    monkeypatch.setattr(transport, "_record_api_usage_async", record)
    async with LLMClient(
        **config, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", request_pool=str(tmp_path), request_capacity=1,
    ) as client:
        operation = client.complete([{"role": "user", "content": "finish"}], on_usage=usages.append)
        if outcome == "success":
            assert (await operation).content == "ok"
        else:
            with pytest.raises(ValueError if outcome == "ledger_error" else asyncio.CancelledError) as caught:
                await operation
            assert caught.value.usage.total_tokens == 4
    async with request_slot(str(tmp_path), 1):
        assert len(usages) == 1


@pytest.mark.parametrize("change", ["remaining", "reduced", "exhausted", "unbounded", "slow_ledger"])
async def test_host_bridge_rechecks_queued_budget_before_native_dispatch(
    tmp_path, monkeypatch, configuration, change,
):
    config, field = configuration
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    first_tokens = 5_000 if change == "remaining" else 20_210

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            entered.set()
            await release.wait()
        total = first_tokens if len(requests) == 1 else 4
        response = completion_http_response(request)
        if request.url.path.endswith("/responses"):
            return http_response(
                request, 200, headers={"content-type": "text/event-stream"},
                text=response.text.replace('"input_tokens": 2', f'"input_tokens": {total - 2}').replace(
                    '"total_tokens": 4', f'"total_tokens": {total}',
                ),
            )
        payload = response.json()
        if config["provider"] == "anthropic":
            payload["usage"]["input_tokens"] = total - 2
        else:
            payload["usage"].update(prompt_tokens=total - 2, total_tokens=total)
        return http_response(request, 200, json=payload)

    install_sdk_transport(monkeypatch, config["provider"], handler)
    record = transport._record_api_usage_async

    async def delayed_record(**kwargs):
        if change == "slow_ledger":
            await asyncio.sleep(0.12)
        await record(**kwargs)

    monkeypatch.setattr(transport, "_record_api_usage_async", delayed_record)
    pool = tmp_path / "pool"
    pool.mkdir()
    artifacts = tmp_path / "artifacts"
    (artifacts / "bridge").mkdir(parents=True)
    environment = LocalEnvironment(str(tmp_path))
    config = {**config, "api_key": "controlled-test",  # pragma: allowlist secret
              "base_url": "https://controlled.invalid/v1",
              "request_pool": str(pool), "request_capacity": 1, "max_output_tokens": 128}
    bridge = AuthoredWorkflowBridge(
        environment, str(artifacts), SessionEnvironmentPool(str(tmp_path), isolation="shared",
                                                          base_environment=environment),
        config, None, allow_unisolated_shell=True, budget=None if change == "unbounded" else 20_000,
    )
    second_messages = [{"role": "user", "content": "second"}]
    if change == "reduced":
        first_tokens = 20_000 - bridge._model().estimate_request_tokens(second_messages) - 50
    native_slot = transport.request_slot
    entries = 0

    @asynccontextmanager
    async def observed_slot(*args):
        nonlocal entries
        entries += 1
        if entries == 2:
            queued.set()
        async with native_slot(*args):
            yield

    monkeypatch.setattr(transport, "request_slot", observed_slot)

    async def complete(identity, messages):
        return await bridge._dispatch(identity, {"action": "complete", "arguments": {
            "messages": messages, "max_output_tokens": 128,
        }})

    first = asyncio.create_task(complete("1", [{"role": "user", "content": "first"}]))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(complete("2", second_messages))
        await asyncio.wait_for(queued.wait(), 5)
        assert len(requests) == 1
        release.set()
        await asyncio.wait_for(first, 5)
        if change in {"exhausted", "slow_ledger"}:
            with pytest.raises(_TokenBudgetStop):
                await asyncio.wait_for(second, 5)
            assert len(requests) == 1
            assert bridge.tokens == first_tokens
        else:
            assert (await asyncio.wait_for(second, 5))["content"] == "ok"
            assert len(requests) == 2
            assert requests[1][field] == (50 if change == "reduced" else 128)
            assert bridge.tokens == first_tokens + 4
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        await bridge.aclose()
        await environment.cleanup()


async def test_host_bridge_charges_reported_error_usage_once_before_rechecking(tmp_path, monkeypatch):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return http_response(
            request, 200, headers={"content-type": "text/event-stream"},
            stream=ReportedUsageStream({"prompt_tokens": 20_200, "completion_tokens": 10, "total_tokens": 20_210},
                                       disconnect=True),
        )

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    environment = LocalEnvironment(str(tmp_path))
    artifacts = tmp_path / "artifacts"
    (artifacts / "bridge").mkdir(parents=True)
    bridge = AuthoredWorkflowBridge(
        environment, str(artifacts), SessionEnvironmentPool(str(tmp_path), isolation="shared",
                                                          base_environment=environment),
        {"model": "gpt-4o", "provider": "openai",
         "api_key": "controlled-test",  # pragma: allowlist secret
         "base_url": "https://controlled.invalid/v1", "request_pool": str(tmp_path), "request_capacity": 1,
         "llm_stream_chat": True, "llm_max_retries": 0},
        None, allow_unisolated_shell=True, budget=20_000,
    )
    try:
        with pytest.raises(Exception) as caught:
            await bridge._dispatch("1", {"action": "complete", "arguments": {
                "messages": [{"role": "user", "content": "first"}], "max_output_tokens": 128,
            }})
        assert caught.value.usage.total_tokens == bridge.tokens == 20_210
        with pytest.raises(_TokenBudgetStop):
            await bridge._dispatch("2", {"action": "complete", "arguments": {
                "messages": [{"role": "user", "content": "second"}], "max_output_tokens": 128,
            }})
        assert len(requests) == 1 and bridge.tokens == 20_210
    finally:
        await bridge.aclose()
        await environment.cleanup()
