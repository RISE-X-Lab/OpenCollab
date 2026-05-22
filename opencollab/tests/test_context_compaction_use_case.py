import asyncio
from pathlib import Path
from types import SimpleNamespace

from opencollab.application.compaction import ContextCompactionUseCase
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.domain.compaction import CompactResult
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


def llm_response(content: str, total_tokens: int = 7):
    return SimpleNamespace(content=content, usage=SimpleNamespace(total_tokens=total_tokens))


class FakeLLM:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def complete(self, messages, temperature=0.0):
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return llm_response("compact summary")


class FakeEventPublisher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class FakeTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, **kwargs):
        self.steps.append(kwargs)


def event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    # Override compaction events with SimpleNamespace shape that the existing
    # assertions expect (no aid in data).
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        compaction=lambda: SimpleNamespace(
            type="compaction",
            data={"reason": "context_overflow"},
        ),
        compaction_applied=lambda tokens_after: SimpleNamespace(
            type="compaction_applied",
            data={"tokens_after": tokens_after},
        ),
        loop_detected=factory.loop_detected,
        tool_start=factory.tool_start,
        tool_end=factory.tool_end,
    )


def messages(count: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "system prompt"},
        *({"role": "user", "content": f"message {idx}"} for idx in range(count)),
    ]


def build_use_case(
    *,
    state=None,
    llm=None,
    event_publisher=None,
    estimate_tokens=None,
    tracer=None,
    compaction_threshold=64_000,
):
    publisher = event_publisher or FakeEventPublisher()
    use_case = ContextCompactionUseCase(
        state=state or SessionState(messages=messages(0)),
        llm=llm or FakeLLM(),
        event_publisher=publisher,
        event_factory=event_factory(),
        estimate_tokens=estimate_tokens or (lambda _messages: 0),
        tracer=tracer,
        compaction_threshold=compaction_threshold,
    )
    return use_case, publisher


def test_context_compaction_use_case_should_compact_uses_injected_token_estimator():
    seen = []

    def estimate_tokens(current_messages):
        seen.append(current_messages)
        return 11

    state = SessionState(messages=messages(1))
    use_case, _publisher = build_use_case(
        state=state,
        estimate_tokens=estimate_tokens,
        compaction_threshold=10,
    )

    assert use_case.should_compact() is True
    assert seen == [state.messages]


def test_context_compaction_use_case_insufficient_messages_only_emits_compaction_event():
    state = SessionState(messages=messages(1))
    llm = FakeLLM()
    use_case, publisher = build_use_case(state=state, llm=llm)
    original_messages = list(state.messages)

    result = run(use_case.compact())

    assert result == CompactResult()
    assert llm.calls == []
    assert state.messages == original_messages
    assert [(event.type, event.data) for event in publisher.events] == [
        ("compaction", {"reason": "context_overflow"})
    ]


def test_context_compaction_use_case_successful_compaction_builds_current_summary_message():
    state = SessionState(messages=messages(10))
    llm = FakeLLM([llm_response("compact summary", total_tokens=7)])
    tracer = FakeTracer()
    use_case, publisher = build_use_case(state=state, llm=llm, tracer=tracer)

    result = run(use_case.compact(apply=True))

    assert result.did_compact is True
    assert result.compacted_count == 2
    assert result.summary_len == len("compact summary")
    assert result.used_tokens_delta == 7
    assert state.messages[1]["content"] == "[Context compacted — summary of 2 earlier messages]:\ncompact summary"
    assert state.used_tokens == 7
    assert [(event.type, event.data) for event in publisher.events] == [
        ("compaction", {"reason": "context_overflow"}),
        ("compaction_applied", {"tokens_after": 7}),
    ]
    assert tracer.steps == [
        {
            "step_type": "compaction",
            "payload": {"messages_compacted": 2, "summary_len": len("compact summary")},
            "tokens": 0,
            "latency": 0,
        }
    ]


def test_context_compaction_use_case_llm_failure_falls_back_to_raw_older_text():
    state = SessionState(messages=messages(10))
    llm = FakeLLM([RuntimeError("summary failed")])
    use_case, _publisher = build_use_case(state=state, llm=llm)

    result = run(use_case.compact(apply=True))

    assert result.used_tokens_delta == 0
    assert state.used_tokens == 0
    assert state.messages[1]["content"].startswith("[Context compacted — summary of 2 earlier messages]:")
    assert "[user]: message 0" in state.messages[1]["content"]
    assert "[user]: message 1" in state.messages[1]["content"]


def test_context_compaction_use_case_apply_false_does_not_mutate_state():
    state = SessionState(messages=messages(10))
    llm = FakeLLM([llm_response("compact summary", total_tokens=7)])
    use_case, publisher = build_use_case(state=state, llm=llm)
    original_messages = list(state.messages)

    result = run(use_case.compact(apply=False))

    assert result.did_compact is True
    assert result.messages is not None
    assert state.messages == original_messages
    assert state.used_tokens == 0
    assert [(event.type, event.data) for event in publisher.events] == [
        ("compaction", {"reason": "context_overflow"})
    ]


def test_context_compaction_use_case_preserves_prompt_tool_call_format():
    older = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "bash"}},
                {"function": {"name": "file_read"}},
            ],
        }
    ]
    use_case, _publisher = build_use_case()

    summary_request, older_text = use_case.build_compaction_prompt(older)

    assert older_text == ["[tool_call]: bash(...)", "[tool_call]: file_read(...)"]
    assert summary_request[1] == {
        "role": "user",
        "content": "[tool_call]: bash(...)\n[tool_call]: file_read(...)",
    }


def test_application_compaction_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/compaction.py").read_text(encoding="utf-8")

    assert "opencollab.core" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.cli" not in source
    assert "opencollab.tui" not in source
    assert "opencollab.team" not in source
