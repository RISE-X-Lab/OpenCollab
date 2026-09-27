"""Session traces distinguish omitted provider reasoning from returned text."""

from __future__ import annotations

import pytest

from tests.support.session_run_loop_test_support import FakeLLM, FakeTracer, build_runner, llm_response, run


@pytest.mark.parametrize("reasoning", [None, ""])
def test_llm_trace_marks_billed_reasoning_that_was_withheld(reasoning):
    response = llm_response(content="answer", reasoning=reasoning)
    response.usage.reasoning_tokens = 135
    tracer = FakeTracer()
    runner = build_runner(llm=FakeLLM([response]), tracer=tracer)

    run(runner.run_loop())

    payload = next(step["payload"] for step in tracer.steps if step["step_type"] == "llm_call")
    assert payload["reasoning_withheld"] is True
    assert payload["usage"]["reasoning_tokens"] == 135
    assert "reasoning" not in payload


@pytest.mark.parametrize("reasoning_tokens", [None, 0])
def test_llm_trace_preserves_shape_for_unbilled_reasoning(reasoning_tokens):
    response = llm_response(content="answer")
    response.usage.reasoning_tokens = reasoning_tokens
    tracer = FakeTracer()
    runner = build_runner(llm=FakeLLM([response]), tracer=tracer)

    run(runner.run_loop())

    payload = next(step["payload"] for step in tracer.steps if step["step_type"] == "llm_call")
    assert "reasoning_withheld" not in payload


def test_llm_trace_preserves_returned_reasoning():
    response = llm_response(content="answer", reasoning="thoughts")
    response.usage.reasoning_tokens = 90
    tracer = FakeTracer()
    runner = build_runner(llm=FakeLLM([response]), tracer=tracer)

    run(runner.run_loop())

    payload = next(step["payload"] for step in tracer.steps if step["step_type"] == "llm_call")
    assert payload["reasoning"] == "thoughts"
    assert "reasoning_withheld" not in payload

