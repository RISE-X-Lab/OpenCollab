"""Unit tests for the read-time summarizer (async->sync bridge for AutoCompact)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import opencollab.application.compaction_summary as compaction_summary
from opencollab.application.compaction_summary import ReadTimeSummarizer, run_coro_blocking

SEGMENT = [
    {"role": "user", "content": "build the thing"},
    {"role": "assistant", "content": "working on it"},
]


def _summarizer(content, *, transcript_path=None, record=None):
    async def acomplete(request):
        if record is not None:
            record.append(request)
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(content=content)

    return ReadTimeSummarizer(acomplete, transcript_path=transcript_path)


def test_run_coro_blocking_returns_result_from_sync_caller():
    async def coro():
        await asyncio.sleep(0)
        return 42

    assert run_coro_blocking(coro) == 42


def test_run_coro_blocking_works_inside_running_loop():
    async def driver():
        # Calls the sync bridge from within an already-running event loop —
        # exactly the situation inside SessionRunUseCase.call_llm.
        return run_coro_blocking(lambda: _identity(7))

    async def _identity(x):
        return x

    assert asyncio.run(driver()) == 7


def test_summarizer_parses_summary_block():
    s = _summarizer("<analysis>scratch</analysis><summary>the gist</summary>")
    out = s(SEGMENT)
    assert "scratch" not in out
    assert out == "Summary:\nthe gist"
    assert s.last_call_cacheable is True


def test_summarizer_sends_the_segment_plus_prompt():
    record: list = []
    s = _summarizer("<summary>ok</summary>", record=record)
    s(SEGMENT)
    request = record[0]
    assert request[:2] == SEGMENT
    assert "detailed summary of the conversation" in request[-1]["content"]


def test_summarizer_appends_transcript_pointer_when_configured():
    s = _summarizer("<summary>done</summary>", transcript_path="/runs/a0.json")
    out = s(SEGMENT)
    assert "/runs/a0.json" in out


def test_summarizer_falls_back_on_llm_error():
    s = _summarizer(RuntimeError("boom"))
    out = s(SEGMENT)
    assert "[user]: build the thing" in out  # bounded raw excerpt
    assert "Summary:" not in out
    assert s.last_call_cacheable is False


def test_summarizer_falls_back_when_no_summary_block():
    # Model returned only an analysis block → format yields empty → fallback.
    s = _summarizer("<analysis>only thinking, forgot the summary</analysis>")
    out = s(SEGMENT)
    assert "[assistant]: working on it" in out
    assert s.last_call_cacheable is False


def test_summarizer_falls_back_when_summary_formatting_fails(monkeypatch):
    def broken_formatter(_raw):
        raise ValueError("broken parser")

    monkeypatch.setattr(compaction_summary, "format_compact_summary", broken_formatter)
    s = _summarizer("<summary>valid provider response</summary>")
    out = s(SEGMENT)
    assert "[user]: build the thing" in out
    assert s.last_call_cacheable is False


def test_summarizer_fallback_is_bounded_and_never_empty():
    big = [{"role": "user", "content": "x" * 100_000}]
    s = _summarizer(RuntimeError("boom"))
    out = s(big)
    assert 0 < len(out) <= 4_000


def test_summarizer_fallback_preserves_supported_content_blocks():
    segment = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "critical requirement"},
                {"type": "image", "source": {"data": "..."}},
                {"type": "input_text", "text": "second requirement"},
                {"type": "custom_attachment", "payload": "..."},
            ],
        },
        {"role": "assistant", "content": ["plain list text"]},
    ]
    out = _summarizer(RuntimeError("boom"))(segment)
    assert "critical requirement" in out
    assert "second requirement" in out
    assert "[image block]" in out
    assert "[custom_attachment block]" in out
    assert "plain list text" in out


def test_summarizer_fallback_keeps_non_text_only_messages_visible():
    segment = [{"role": "user", "content": [{"type": "image_url", "image_url": "..."}]}]
    out = _summarizer(RuntimeError("boom"))(segment)
    assert out == "[user]: [image_url block]"


def test_composed_summarizer_closes_each_ephemeral_client(monkeypatch):
    from opencollab.bootstrap import container

    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.close_calls = 0
            clients.append(self)

        async def complete(self, _request, *, temperature):
            assert temperature == 0.0
            return SimpleNamespace(content="<summary>closed cleanly</summary>")

        async def close(self):
            self.close_calls += 1

    monkeypatch.setattr(container, "LLMClient", FakeClient)
    agent = SimpleNamespace(
        model="model",
        api_key="key",  # pragma: allowlist secret
        base_url=None,
        provider="openai",
    )
    summarizer = container._build_summarizer(
        agent,
        None,
        resolved_llm=object(),
        llm_timeout=1.0,
        auto_save_path=None,
    )

    assert summarizer(SEGMENT) == "Summary:\nclosed cleanly"
    assert len(clients) == 1
    assert clients[0].close_calls == 1
