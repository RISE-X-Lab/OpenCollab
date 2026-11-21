from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import Usage, retry
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.adapters.llm.types import LLMResponse
from opencollab.application.run_control import RunControl
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.tools import builtin_tools
from tests.runtime.test_responses_error_usage_http import _payload
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _serialized_usage(usage):
    return json.loads(json.dumps(asdict(usage)))


def _serialized_response(response):
    values = json.loads(json.dumps(asdict(response)))
    values["usage"] = Usage(**values["usage"])
    return LLMResponse(**values)


@dataclass
class _ReportingUsage(Usage):
    source: str = " responses "

    def __post_init__(self):
        self.source = self.source.strip()


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("forward_fallback", [False, True])
@pytest.mark.parametrize("scenario", [
    "primary", "copied-primary", "scrubbed-primary", "replaced-primary",
    "fallback", "equal-fallback", "copied-fallback", "scrubbed-fallback", "replaced-fallback",
    "annotated-primary", "annotated-fallback", "annotated-primary-after-helper",
    "annotated-primary-after-helper-without-timing",
    "cache-redacted-primary-after-helper", "cache-redacted-primary-after-helper-without-timing",
    "cache-redacted-fallback", "fallback-with-primary-timing", "manual-primary-settlement",
    "manual-scrubbed-primary-settlement", "manual-count-adjustment", "recovered-primary-after-redaction",
    "recovered-primary-after-roundtrip",
    "recovered-primary-after-roundtrip-with-helper",
    "recovered-primary-after-roundtrip-with-fallback",
    "recovered-primary-after-subclass-roundtrip",
    "fallback-with-primary-timing-and-rebuilt-usage",
    "failed-primary", "failed-primary-attempt-callback-only",
    "serialized-primary", "serialized-equal-fallback",
    "failed-primary-serialized-fallback", "serialized-fallback-with-stale-timing",
])
async def test_routed_completion_accounts_for_each_provider(
    tmp_path, monkeypatch, summary, forward_fallback, scenario,
):
    requests = []
    events = []
    answer = "<summary>Answer.</summary>" if summary else "Answer."
    recovered_primary = scenario.startswith("recovered-primary-")
    recovered_helper = scenario == "recovered-primary-after-roundtrip-with-helper"
    recovered_fallback = scenario == "recovered-primary-after-roundtrip-with-fallback"
    rebuilt_usage = "roundtrip" in scenario or scenario.endswith("-rebuilt-usage")
    independent_helper = "-primary-after-helper" in scenario or recovered_helper
    cache_redacted = scenario.startswith("cache-redacted-")
    annotated = scenario.startswith("annotated-") or cache_redacted
    retains_primary_timing = scenario.startswith("fallback-with-primary-timing")
    serialized = "serialized" in scenario
    stale_timing = scenario == "serialized-fallback-with-stale-timing"
    manual_primary_settlement = recovered_primary or scenario in {
        "manual-primary-settlement", "manual-scrubbed-primary-settlement", "manual-count-adjustment",
    }
    adjusted_counts = scenario == "manual-count-adjustment"
    usable_primary = independent_helper or (recovered_primary and not recovered_fallback) or scenario in {
        "primary", "copied-primary", "scrubbed-primary", "replaced-primary",
        "annotated-primary", "manual-count-adjustment", "serialized-primary",
    }
    equal_usage = independent_helper or retains_primary_timing or scenario in {
        "equal-fallback", "copied-fallback", "scrubbed-fallback", "replaced-fallback",
        "annotated-fallback", "cache-redacted-fallback", "manual-primary-settlement",
        "manual-scrubbed-primary-settlement", "serialized-equal-fallback", "serialized-fallback-with-stale-timing",
    }
    failed_primary = scenario.startswith("failed-primary")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry.random, "uniform", lambda _lo, _hi: 0.0)

    async def handle(request):
        route = request.url.path.split("/")[1]
        requests.append(route)
        if route == "primary":
            if failed_primary:
                content_type, payload = _payload(json.loads(request.content), {"kind": "failed"})
                return http_response(request, 200, headers={"content-type": content_type}, content=payload)
            if recovered_primary:
                spec = {"kind": "failed"} if len(requests) == 1 else {
                    "text": answer, "usage": {
                        "input_tokens": 2, "output_tokens": 55, "total_tokens": 57,
                        "input_tokens_details": {"cached_tokens": 1},
                    },
                }
                content_type, payload = _payload(json.loads(request.content), spec)
                return http_response(request, 200, headers={"content-type": content_type}, content=payload)
        text = answer if route != "primary" or usable_primary else ""
        output_tokens = 55 if route == "primary" or equal_usage else 2
        payload = completion_http_response(request, text=text, output_tokens=output_tokens).json()
        if recovered_helper or recovered_fallback:
            payload["usage"].update(prompt_tokens=32, completion_tokens=62, total_tokens=94)
        payload["usage"]["prompt_tokens_details"] = {
            "cached_tokens": 0 if retains_primary_timing and route == "fallback" else 1,
        }
        return http_response(request, 200, json=payload)

    def copy_response(response):
        if serialized:
            return _serialized_response(response)
        if scenario.startswith("replaced-"):
            return replace(response, usage=replace(response.usage, raw_usage={}))
        if annotated:
            removes_timing = scenario.endswith("-without-timing")
            usage = replace(
                response.usage,
                context_tokens=7,
                context_output_tokens=5,
                context_tokens_estimated=True,
                cache_read_tokens=None if cache_redacted else response.usage.cache_read_tokens,
                raw_usage=response.usage.raw_usage if removes_timing else {},
            )
            timing = None if removes_timing else response.transport_timing
            return replace(response, usage=usage, transport_timing=timing)
        copied = deepcopy(response)
        if scenario.startswith("scrubbed-"):
            copied.usage.raw_usage.clear()
        return copied

    class FallbackClient(LLMClient):
        async def complete(
            self, messages, *, on_admitted=None, before_request=None,
            on_attempt_usage=None, on_usage=None, **kwargs,
        ):
            controls = dict(on_admitted=on_admitted, before_request=before_request,
                            on_attempt_usage=on_attempt_usage, on_usage=on_usage)
            primary_controls = dict(controls)
            if manual_primary_settlement or scenario == "failed-primary-attempt-callback-only":
                primary_controls.pop("on_usage")
            try:
                first = await super().complete(messages, **kwargs, **primary_controls)
            except ResponsesProtocolError:
                assert failed_primary
            else:
                if manual_primary_settlement and on_usage is not None:
                    changes = {"cache_read_tokens": None}
                    if scenario != "manual-primary-settlement":
                        changes["raw_usage"] = {}
                    if adjusted_counts:
                        changes["output_tokens"] = first.usage.output_tokens + 1
                    if rebuilt_usage:
                        assert first.usage.cache_read_tokens == 1
                        values = _serialized_usage(first.usage)
                        cls = _ReportingUsage if "subclass" in scenario else Usage
                        cleaned = cls(**(values | changes))
                        if isinstance(cleaned, _ReportingUsage):
                            assert cleaned.source == "responses"
                        if recovered_helper:
                            await fallback.complete(messages, **kwargs)
                    else:
                        cleaned = replace(first.usage, **changes)
                    on_usage(cleaned)
                    if recovered_primary and not recovered_fallback:
                        return replace(first, usage=cleaned)
                if not recovered_fallback and first.content and first.content.strip():
                    if independent_helper:
                        await fallback.complete(messages, **kwargs)
                    return copy_response(first) if scenario != "primary" else first
            fallback_controls = dict(controls) if forward_fallback else {}
            if stale_timing:
                fallback_controls.pop("on_usage", None)
            final = await fallback.complete(messages, **kwargs, **fallback_controls)
            if stale_timing:
                return copy_response(replace(final, transport_timing=first.transport_timing))
            if retains_primary_timing:
                usage = replace(final.usage)
                if rebuilt_usage:
                    usage = Usage(**(_serialized_usage(usage) | {"raw_usage": {}}))
                return replace(final, usage=usage, transport_timing=first.transport_timing)
            if failed_primary or annotated or serialized or scenario in {
                "copied-fallback", "scrubbed-fallback", "replaced-fallback",
            }:
                return copy_response(final)
            return final

    install_sdk_transport(monkeypatch, "openai", handle)
    options = dict(api_key=__name__, context_window=40_000, request_pool=str(tmp_path), request_capacity=1)
    control = RunControl(on_event=events.append)
    async with (
        FallbackClient(
            **options, base_url="https://test.invalid/primary/v1",
            wire_protocol="responses" if failed_primary or recovered_primary else "chat_completions",
            max_retries=1 if failed_primary or recovered_primary else 0,
        ) as client,
        LLMClient(**options, base_url="https://test.invalid/fallback/v1", max_retries=0) as fallback,
    ):
        if summary:
            env = LocalEnvironment(str(tmp_path))
            session = build_session(
                agent=Agent(name="routed", system_prompt="System.", max_tokens_per_step=128), llm=client,
                env=env, llm_timeout=2, max_budget_tokens=20_000, run_control=control,
            )
            try:
                await session.add_user_message("Return a brief summary.")
                text = await session._context_summarizer.asummarize(session.messages)
                assert text == "Summary:\nAnswer."
                assert session._context_summarizer.last_call_cacheable
                if annotated:
                    assert session.state.context_tokens == 7
                tokens = session.used_tokens
            finally:
                await session.aclose()
                await env.cleanup()
        else:
            result = await OpenCollab(tmp_path, config={"llm_timeout": 2, "max_output_tokens": 128}).agent(
                "Return a brief answer.", llm=client, tools=(), budget=20_000, trace=False,
                run_control=control, system_prompt="System.",
            )
            assert result.status == "completed", result.reason
            assert result.output == "Answer."
            tokens = result.tokens

    expected_usage = [94 if recovered_primary else 74 if failed_primary else 57]
    expected_ledger = expected_usage.copy()
    if adjusted_counts:
        expected_usage.insert(0, 58)
    if not usable_primary:
        expected_usage.append(94 if recovered_fallback else 57 if equal_usage else 4)
        expected_ledger.append(expected_usage[-1])
    expected_purpose = "summary" if summary else "completion"
    ledger = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    expected_requests = ["primary", "primary"] if failed_primary or recovered_primary else ["primary"]
    if not usable_primary or independent_helper:
        expected_requests.append("fallback")
    if independent_helper:
        expected_ledger.append(94 if recovered_helper else 57)
    assert requests == expected_requests
    assert tokens == sum(expected_usage)
    assert [entry["usage"]["total_tokens"] for entry in ledger] == expected_ledger
    usage_events = [event for event in events if event.type == "usage"]
    assert [event.data["total_tokens"] for event in usage_events] == expected_usage
    assert {event.data["purpose"] for event in usage_events} == {expected_purpose}
    if rebuilt_usage and recovered_primary:
        assert ledger[0]["usage"]["cached_input_tokens"] == 1
        assert usage_events[0].data["cache_read_tokens"] is None
    if failed_primary:
        assert [entry["status"] for entry in ledger] == ["error", "success"]
        assert [event.data["error_type"] for event in usage_events] == ["ResponsesTransientEventError", None]


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("retried", [False, True])
async def test_manual_usage_copy_is_reconciled_before_the_next_request(tmp_path, monkeypatch, parallel, retried):
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_path))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry.random, "uniform", lambda _lo, _hi: 0.0)
    requests = []
    events = []
    input_tokens, output_tokens = (32, 62) if retried else (2, 55)
    each_total = input_tokens + output_tokens

    async def handle(request):
        route = request.url.path.split("/")[1]
        body = json.loads(request.content)
        is_primary_retry = route == "primary" and any(earlier == "primary" for earlier, _ in requests)
        requests.append((route, body.get("max_output_tokens", body.get("max_tokens"))))
        if route == "primary":
            spec = {"kind": "failed"} if retried and not is_primary_retry else {"text": "Primary answer.", "usage": {
                "input_tokens": 2, "output_tokens": 55, "total_tokens": 57,
                "input_tokens_details": {"cached_tokens": 1},
            }}
            content_type, payload = _payload(body, spec)
            return http_response(request, 200, headers={"content-type": content_type}, content=payload)
        if route == "followup":
            return completion_http_response(request, output_tokens=13)
        payload = completion_http_response(request, text="Other answer.", output_tokens=output_tokens).json()
        payload["usage"].update(prompt_tokens=input_tokens, total_tokens=each_total)
        return http_response(request, 200, json=payload)

    class RoutedClient(LLMClient):
        async def complete(
            self, messages, *, on_admitted=None, before_request=None, on_attempt_usage=None, on_usage=None, **kwargs,
        ):
            controls = dict(on_admitted=on_admitted, before_request=before_request, on_attempt_usage=on_attempt_usage)

            async def primary():
                return await super(RoutedClient, self).complete(messages, **kwargs, **controls)

            if parallel:
                response, _ = await asyncio.gather(
                    primary(), alternate.complete(messages, **kwargs, **controls, on_usage=on_usage),
                )
            else:
                response = await primary()
                await alternate.complete(messages, **kwargs, **controls, on_usage=on_usage)
            fields = asdict(response.usage)
            assert vars(response.usage) == fields
            public_usage = Usage(**fields)
            assert public_usage == response.usage == public_usage
            redacted = replace(response.usage, raw_usage={}, cache_read_tokens=None)
            assert on_usage is not None
            on_usage(deepcopy(redacted) if parallel else redacted)
            await followup.complete(messages, **kwargs, **controls, on_usage=on_usage)
            return response

    install_sdk_transport(monkeypatch, "openai", handle)
    options = dict(api_key=__name__, context_window=40_000, request_pool=str(tmp_path), request_capacity=2)
    async with (
        RoutedClient(**options, base_url="https://test.invalid/primary/v1", wire_protocol="responses",
                     max_retries=int(retried)) as client,
        LLMClient(**options, base_url="https://test.invalid/alternate/v1", max_retries=0) as alternate,
        LLMClient(**options, base_url="https://test.invalid/followup/v1", max_retries=0) as followup,
    ):
        result = await OpenCollab(tmp_path, config={"llm_timeout": 3, "max_output_tokens": 900}).agent(
            "Give one clear answer.", llm=client, tools=(), budget=1_020, trace=False,
            run_control=RunControl(on_event=events.append), system_prompt="System.",
        )

    assert result.status == "completed", result.reason
    assert result.output == "Primary answer."
    assert result.tokens == each_total * 2 + 15
    assert sorted(route for route, _ in requests[:-1]) == ["alternate", *(["primary"] * (1 + retried))]
    assert requests[-1] == ("followup", requests[0][1] - each_total * 2)
    ledger = [json.loads(line) for line in usage_path.read_text().splitlines()]
    assert [entry["usage"]["total_tokens"] for entry in ledger] == [each_total, each_total, 15]
    usage_events = [event.data for event in events if event.type == "usage"]
    assert [event["total_tokens"] for event in usage_events] == [each_total, each_total, 15]
    assert [event["cache_read_tokens"] for event in usage_events].count(None) == 1


@pytest.mark.parametrize("cache_read_tokens", [None, 0])
@pytest.mark.parametrize("redact_at", [
    "response", "settlement", "wrapper", "wrapper-redacted", "wrapper-copied", "wrapper-retry-roundtrip",
    "serialized-retry",
])
async def test_cache_redaction_preserves_budget_for_tool_followup(tmp_path, monkeypatch, cache_read_tokens, redact_at):
    (tmp_path / "facts.txt").write_text("The verified value is 42.\n", encoding="utf-8")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []
    events = []
    retried = redact_at in {"wrapper-retry-roundtrip", "serialized-retry"}
    limits = []
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry.random, "uniform", lambda _lo, _hi: 0.0)

    async def handle(request):
        body = json.loads(request.content)
        if retried:
            tool_results = [item["output"] for item in body["input"] if item.get("type") == "function_call_output"]
            requests.append(tool_results)
            limits.append(body["max_output_tokens"])
            input_tokens, output_tokens = (315, 22) if tool_results else (233, 30)
            spec = {"text": "The value is 42.", "usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens, "input_tokens_details": {"cached_tokens": 1},
            }}
            if not tool_results:
                spec["function_arguments"] = json.dumps({"path": "facts.txt"})
            if len(requests) == 1:
                spec = {"kind": "failed"}
            content_type, payload = _payload(body, spec)
            payload = payload.replace(b'"name": "structured_output"', b'"name": "file_read"')
            return http_response(request, 200, headers={"content-type": content_type}, content=payload)
        tool_results = [message["content"] for message in body["messages"] if message["role"] == "tool"]
        requests.append(tool_results)
        input_tokens, output_tokens = (315, 22) if tool_results else (270, 30)
        assert body["max_tokens"] >= output_tokens
        payload = completion_http_response(request, text="The value is 42.", output_tokens=output_tokens).json()
        payload["usage"].update(
            prompt_tokens=input_tokens, total_tokens=input_tokens + output_tokens,
            prompt_tokens_details={"cached_tokens": 1},
        )
        if not tool_results:
            choice = payload["choices"][0]
            choice["finish_reason"] = "tool_calls"
            choice["message"].update(content=None, tool_calls=[{
                "id": "read-facts",
                "type": "function",
                "function": {"name": "file_read", "arguments": json.dumps({"path": "facts.txt"})},
            }])
        return http_response(request, 200, json=payload)

    class CacheRedactingClient(LLMClient):
        async def complete(
            self, messages, *, on_admitted=None, before_request=None, on_attempt_usage=None, on_usage=None, **kwargs,
        ):
            def redact_usage(usage):
                on_usage(replace(usage, cache_read_tokens=cache_read_tokens))

            settlement_callback = on_usage
            if redact_at == "settlement" and on_usage is not None:
                settlement_callback = redact_usage
            elif redact_at.startswith("wrapper"):
                settlement_callback = None
            controls = dict(on_admitted=on_admitted, before_request=before_request,
                            on_attempt_usage=on_attempt_usage, on_usage=settlement_callback)
            kwargs.update((name, callback) for name, callback in controls.items() if callback is not None)
            response = await super().complete(messages, **kwargs)
            assert response.usage.cache_read_tokens == 1
            if redact_at == "serialized-retry":
                redacted = replace(response.usage, cache_read_tokens=cache_read_tokens, raw_usage={})
                return _serialized_response(replace(response, usage=redacted))
            if redact_at.startswith("wrapper") and on_usage is not None:
                if redact_at == "wrapper":
                    redact_usage(response.usage)
                else:
                    changes = {"cache_read_tokens": cache_read_tokens, "raw_usage": {}}
                    if retried:
                        redacted = Usage(**(_serialized_usage(response.usage) | changes))
                    else:
                        redacted = replace(response.usage, **changes)
                    on_usage(redacted)
                    if redact_at == "wrapper-copied":
                        redacted = deepcopy(redacted)
                    return replace(response, usage=redacted)
            if redact_at == "response":
                return replace(response, usage=replace(response.usage, cache_read_tokens=cache_read_tokens))
            return response

    install_sdk_transport(monkeypatch, "openai", handle)
    async with CacheRedactingClient(
        api_key=__name__, base_url="https://test.invalid/v1", max_retries=int(retried),
        wire_protocol="responses" if retried else "chat_completions",
        context_window=40_000, request_pool=str(tmp_path), request_capacity=1,
    ) as client:
        result = await OpenCollab(tmp_path, config={"llm_timeout": 3, "max_output_tokens": 900}).agent(
            "Read facts.txt and report the verified value.", llm=client, tools=builtin_tools("file_read"),
            budget=1_250, max_steps=4, trace=False, system_prompt="System.",
            run_control=RunControl(on_event=events.append),
        )

    assert result.status == "completed", result.reason
    assert result.output == "The value is 42."
    assert result.tokens == 637
    assert requests[:-1] == ([[], []] if retried else [[]])
    assert "The verified value is 42." in requests[-1][0]
    if retried:
        assert limits[1] == limits[0] - 37
    ledger = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [entry["usage"]["total_tokens"] for entry in ledger] == [300, 337]
    usage_events = [event.data for event in events if event.type == "usage"]
    assert [event["total_tokens"] for event in usage_events] == [300, 337]
    if redact_at not in {"response", "serialized-retry"}:
        assert [event["cache_read_tokens"] for event in usage_events] == [cache_read_tokens] * 2
