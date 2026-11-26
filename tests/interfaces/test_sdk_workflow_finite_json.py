"""Structured workflow results and their traces can be consumed as JSON."""

import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from tests.support.provider_sdk_http import (
    completion_http_response,
    http_response,
    install_sdk_transport,
)


def _reject_constant(value):
    raise ValueError(f"non-JSON number {value}")


def _assert_json_artifacts(artifacts):
    for path in artifacts.glob("*.json"):
        json.loads(path.read_text(), parse_constant=_reject_constant)
    for path in artifacts.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            json.loads(line, parse_constant=_reject_constant)


@pytest.mark.parametrize(("provider", "number"), [
    ("openai", "2.5"), ("openai", "NaN"), ("openai", "1e309"),
    ("anthropic", "2.5"), ("anthropic", "1e309"),
])
async def test_workflow_repairs_unrepresentable_nested_tool_numbers(tmp_path, monkeypatch, provider, number):
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2
        value = number if len(requests) == 1 else "null"
        response = completion_http_response(request).json()
        if provider == "anthropic":
            response["stop_reason"] = "tool_use"
            response["content"] = [{
                "id": f"capture-{len(requests)}",
                "type": "tool_use",
                "name": "structured_output",
                "input": {"samples": [{"mean": "__fixture_mean__"}]},
            }]
            raw = json.dumps(response).replace('"__fixture_mean__"', value)
            return http_response(request, 200, headers={"content-type": "application/json"}, content=raw)
        choice = response["choices"][0]
        choice["finish_reason"] = "tool_calls"
        choice["message"].update(content=None, tool_calls=[{
            "id": f"capture-{len(requests)}",
            "type": "function",
            "function": {
                "name": "structured_output",
                "arguments": '{"samples":[{"mean":' + value + '}]}',
            },
        }])
        return http_response(request, 200, json=response)

    install_sdk_transport(monkeypatch, provider, reply)
    artifacts = tmp_path / "artifacts"
    output = tmp_path / "report.json"
    schema = {"type": "object", "properties": {"samples": {"type": "array"}}, "required": ["samples"]}

    async def flow(ctx, _inputs):
        report = await ctx.agent("Report the measurements.", tools=[], schema=schema)
        output.write_text(json.dumps(report, allow_nan=False))
        return report

    result = await OpenCollab(
        tmp_path, provider=provider, model="offline-measurement",
        api_key=__name__, base_url="https://offline.invalid/v1",
    ).workflow(flow, artifacts=artifacts, trace=True, timeout=10)

    assert result.status == "completed"
    repaired = number != "2.5"
    expected = {"samples": [{"mean": None if repaired else 2.5}]}
    assert result.output == json.loads(output.read_text(), parse_constant=_reject_constant) == expected
    assert len(requests) == (2 if repaired else 1)
    assert result.tokens == 4 * len(requests)
    if repaired:
        assert "Error: invalid JSON arguments" in json.dumps(requests[1]["messages"])
    _assert_json_artifacts(artifacts)


@pytest.mark.parametrize("finite", [False, True])
async def test_workflow_records_preparsed_arguments_as_text(tmp_path, finite):
    calls = 0

    class Model:
        async def complete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            mean = 2.5 if finite else (float("nan") if calls == 1 else None)
            return LLMResponse(
                finish_reason="tool_calls", usage=Usage(2, 2),
                tool_calls=[{
                    "id": f"capture-{calls}",
                    "type": "function",
                    "function": {
                        "name": "structured_output",
                        "arguments": {"samples": [{"mean": mean}]},
                    },
                }],
            )

    schema = {"type": "object", "properties": {"samples": {"type": "array"}}, "required": ["samples"]}
    artifacts = tmp_path / "artifacts"

    async def flow(ctx, _inputs):
        return await ctx.agent("Report the samples.", schema=schema, tools=[])

    result = await OpenCollab(tmp_path, model="offline").workflow(
        flow, llm=Model(), artifacts=artifacts, trace=True, timeout=10,
    )

    assert result.status == "completed"
    assert result.output == {"samples": [{"mean": 2.5 if finite else None}]}
    assert calls == (1 if finite else 2)
    assert result.tokens == 4 * calls
    _assert_json_artifacts(artifacts)
