from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm import client as client_module
from opencollab.adapters.llm import usage_ledger as ledger
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import Usage
from opencollab.adapters.llm.usage_ledger import (
    append_usage_record,
    build_usage_record,
    usage_cost_usd,
    usage_log_path,
)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_glm_usage_cost_accounts_for_cache_discount(monkeypatch):
    monkeypatch.delenv("GLM_INPUT_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("GLM_CACHED_INPUT_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("GLM_CACHE_CREATION_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("GLM_OUTPUT_USD_PER_MTOK", raising=False)
    usage = Usage(
        input_tokens=1000,
        output_tokens=50,
        cache_read_tokens=600,
        cache_creation_tokens=200,
    )

    cost = usage_cost_usd(usage, "glm-5.2")

    expected = (200 * 1.4 + 600 * 0.26 + 200 * 1.4 + 50 * 4.4) / 1_000_000
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


def test_usage_log_is_disabled_without_explicit_path(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_API_USAGE_LOG", raising=False)
    monkeypatch.chdir(tmp_path)

    append_usage_record({"schema": "opencollab.api_usage.v1"})

    assert usage_log_path() is None
    assert not (tmp_path / ".opencollab").exists()


def test_usage_record_strips_base_url_userinfo_and_query():
    record = build_usage_record(
        provider="openai",
        model="glm-5.2",
        base_url="https://user:secret@example.com:8443/v1?api_key=hidden",
        latency_s=0.25,
        status="success",
    )
    record_text = json.dumps(record)

    assert record["base_url"] == "https://example.com:8443"
    assert record["base_url_host"] == "example.com:8443"
    assert "user" not in record_text
    assert "secret" not in record_text
    assert "hidden" not in record_text


def test_error_message_redacts_secrets_without_hiding_plain_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-secret-token")
    error = RuntimeError(
        "failed https://user:secret@example.com:8443/v1?token=hidden "
        "token=abc123 prompt=ordinary prompt content message=plain input "
        "bearer local-secret-token"
    )

    record = build_usage_record(
        provider="openai",
        model="glm-5.2",
        base_url="https://safe.example/v1",
        latency_s=0.25,
        status="error",
        error=error,
    )
    record_text = json.dumps(record)

    assert "https://example.com:8443" in record_text
    assert "user" not in record_text
    assert "hidden" not in record_text
    assert "abc123" not in record_text
    assert "ordinary prompt content" in record_text
    assert "message=plain input" in record_text
    assert "local-secret-token" not in record_text


def test_error_message_redacts_quoted_secret_assignments():
    error = RuntimeError(
        "failed password=\"my-secret\" {'token': 'abc123'} "
        '"secret": "top secret value" prompt=ordinary prompt'
    )

    record = build_usage_record(
        provider="openai",
        model="glm-5.2",
        base_url="https://safe.example/v1",
        latency_s=0.25,
        status="error",
        error=error,
    )
    message = record["error"]["message"]

    assert 'password="[redacted]"' in message
    assert "'token': '[redacted]'" in message
    assert '"secret": "[redacted]"' in message
    assert "ordinary prompt" in message
    assert "my-secret" not in message
    assert "abc123" not in message
    assert "top secret value" not in message


def test_record_api_usage_is_fail_safe(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("ledger build failed")

    monkeypatch.setattr(ledger, "build_usage_record", boom)

    ledger.record_api_usage(
        provider="openai",
        model="glm-5.2",
        base_url="http://127.0.0.1:18788/v1",
        latency_s=0.1,
        status="success",
    )


def test_async_usage_recording_runs_under_lock(monkeypatch):
    events = []

    class FakeLock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    async def fake_to_thread(fn):
        events.append("to_thread")
        fn()

    def fake_record_api_usage(**kwargs):
        events.append("record")

    monkeypatch.setattr(client_module, "_ledger_lock", FakeLock())
    monkeypatch.setattr(client_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(client_module, "record_api_usage", fake_record_api_usage)

    asyncio.run(
        client_module._record_api_usage_async(
            provider="openai",
            model="glm-5.2",
            base_url="http://127.0.0.1:18788/v1",
            latency_s=0.1,
            status="success",
        )
    )

    assert events == ["to_thread", "enter", "record", "exit"]


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
