from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import retry
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.application.run_control import RunControl
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from tests.runtime.test_responses_error_usage_http import _payload
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("forward_fallback", [False, True])
@pytest.mark.parametrize("scenario", [
    "primary", "copied-primary", "scrubbed-primary", "replaced-primary",
    "fallback", "equal-fallback", "copied-fallback", "scrubbed-fallback", "replaced-fallback",
    "annotated-primary", "annotated-fallback", "annotated-primary-after-helper",
    "annotated-primary-after-helper-without-timing",
    "failed-primary", "failed-primary-attempt-callback-only",
])
async def test_routed_completion_accounts_for_each_provider(
    tmp_path, monkeypatch, summary, forward_fallback, scenario,
):
    requests = []
    events = []
    answer = "<summary>Answer.</summary>" if summary else "Answer."
    independent_helper = scenario.startswith("annotated-primary-after-helper")
    usable_primary = independent_helper or scenario in {
        "primary", "copied-primary", "scrubbed-primary", "replaced-primary",
        "annotated-primary",
    }
    equal_usage = independent_helper or scenario in {
        "equal-fallback", "copied-fallback", "scrubbed-fallback", "replaced-fallback", "annotated-fallback",
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
            return completion_http_response(request, text=answer if usable_primary else "", output_tokens=55)
        return completion_http_response(request, text=answer, output_tokens=55 if equal_usage else 2)

    def copy_response(response):
        if scenario.startswith("replaced-"):
            return replace(response, usage=replace(response.usage, raw_usage={}))
        if scenario.startswith("annotated-"):
            removes_timing = scenario == "annotated-primary-after-helper-without-timing"
            usage = replace(
                response.usage,
                context_tokens=7,
                context_output_tokens=5,
                context_tokens_estimated=True,
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
            if scenario == "failed-primary-attempt-callback-only":
                primary_controls.pop("on_usage")
            try:
                first = await super().complete(messages, **kwargs, **primary_controls)
            except ResponsesProtocolError:
                assert failed_primary
            else:
                if first.content and first.content.strip():
                    if independent_helper:
                        await fallback.complete(messages, **kwargs)
                    return copy_response(first) if scenario != "primary" else first
            final = await fallback.complete(messages, **kwargs, **(controls if forward_fallback else {}))
            if failed_primary or scenario in {
                "copied-fallback", "scrubbed-fallback", "replaced-fallback", "annotated-fallback",
            }:
                return copy_response(final)
            return final

    install_sdk_transport(monkeypatch, "openai", handle)
    options = dict(api_key=__name__, context_window=40_000, request_pool=str(tmp_path), request_capacity=1)
    control = RunControl(on_event=events.append)
    async with (
        FallbackClient(
            **options, base_url="https://test.invalid/primary/v1",
            wire_protocol="responses" if failed_primary else "chat_completions",
            max_retries=1 if failed_primary else 0,
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
                if scenario.startswith("annotated-"):
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

    expected_usage = [74 if failed_primary else 57]
    if not usable_primary:
        expected_usage.append(57 if equal_usage else 4)
    expected_purpose = "summary" if summary else "completion"
    ledger = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    expected_requests = ["primary", "primary"] if failed_primary else ["primary"]
    if not usable_primary or independent_helper:
        expected_requests.append("fallback")
    assert requests == expected_requests
    assert tokens == sum(expected_usage)
    assert [entry["usage"]["total_tokens"] for entry in ledger] == expected_usage + ([57] if independent_helper else [])
    usage_events = [event for event in events if event.type == "usage"]
    assert [event.data["total_tokens"] for event in usage_events] == expected_usage
    assert {event.data["purpose"] for event in usage_events} == {expected_purpose}
    if failed_primary:
        assert [entry["status"] for entry in ledger] == ["error", "success"]
        assert [event.data["error_type"] for event in usage_events] == ["ResponsesTransientEventError", None]
