from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import Usage
from opencollab.adapters.llm.usage_ledger import (
    append_usage_record,
    build_usage_record,
    usage_cost_usd,
)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_glm_usage_cost_accounts_for_cache_discount(monkeypatch):
    monkeypatch.delenv("GLM_INPUT_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("GLM_CACHED_INPUT_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("GLM_OUTPUT_USD_PER_MTOK", raising=False)
    usage = Usage(input_tokens=1000, output_tokens=50, cache_read_tokens=800)

    cost = usage_cost_usd(usage, "glm-5.2")

    expected = (200 * 1.4 + 800 * 0.26 + 50 * 4.4) / 1_000_000
    assert cost == pytest.approx(expected)


def test_append_usage_record_writes_jsonl(tmp_path):
    path = tmp_path / "api_usage.jsonl"
    record = build_usage_record(
        provider="openai",
        model="glm-5.2",
        base_url="http://127.0.0.1:18788/v1?token=hidden",
        latency_s=0.25,
        status="success",
    )

    append_usage_record(record, path)

    rows = _read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["schema"] == "opencollab.api_usage.v1"
    assert rows[0]["base_url"] == "http://127.0.0.1:18788"
    assert "hidden" not in json.dumps(rows[0])


class _FakeCompletions:
    async def create(self, **kwargs):
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        )
        message = SimpleNamespace(content="hi", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage)


class _BoomCompletions:
    async def create(self, **kwargs):
        raise RuntimeError("upstream failed bearer local-secret-token")


def test_llm_client_records_success_without_prompt_or_key(tmp_path, monkeypatch):
    path = tmp_path / "api_usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "local-secret-token")
    client = LLMClient(
        model="glm-5.2",
        provider="openai",
        base_url="http://127.0.0.1:18788/v1",
        max_retries=0,
    )
    client._openai = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    response = asyncio.run(client.complete([{"role": "user", "content": "very secret prompt"}]))

    assert response.content == "hi"
    rows = _read_jsonl(path)
    assert len(rows) == 1
    record_text = json.dumps(rows[0])
    assert rows[0]["status"] == "success"
    assert rows[0]["usage"]["input_tokens"] == 100
    assert rows[0]["usage"]["cached_input_tokens"] == 40
    assert rows[0]["usage"]["output_tokens"] == 20
    assert "very secret prompt" not in record_text
    assert "local-secret-token" not in record_text


def test_llm_client_records_error_with_redaction(tmp_path, monkeypatch):
    path = tmp_path / "api_usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "local-secret-token")
    client = LLMClient(
        model="glm-5.2",
        provider="openai",
        base_url="http://127.0.0.1:18788/v1",
        max_retries=0,
    )
    client._openai = SimpleNamespace(chat=SimpleNamespace(completions=_BoomCompletions()))

    with pytest.raises(RuntimeError):
        asyncio.run(client.complete([{"role": "user", "content": "very secret prompt"}]))

    rows = _read_jsonl(path)
    assert len(rows) == 1
    record_text = json.dumps(rows[0])
    assert rows[0]["status"] == "error"
    assert rows[0]["error"]["type"] == "RuntimeError"
    assert "[redacted]" in rows[0]["error"]["message"]
    assert "very secret prompt" not in record_text
    assert "local-secret-token" not in record_text
