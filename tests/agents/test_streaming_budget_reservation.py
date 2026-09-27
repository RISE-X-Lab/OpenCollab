"""Streaming budget reservation follows the reasoning fields actually sent."""

from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.token_estimation import (
    estimate_request_message_tokens,
    estimate_request_tokens,
)
from tests.support.session_run_loop_test_support import FakeAgent, FakeLLM, build_runner, llm_response, run

# --------------------------------------------------------------------------- #
# reasoning_content is not resent when streaming, so it must not be reserved
# --------------------------------------------------------------------------- #


def _reasoning_heavy_messages(reasoning_chars: int = 600_000):
    """A history whose bulk is recorded chain-of-thought, as the batches had.

    Streaming is the only way ``reasoning_content`` becomes non-empty, and the
    outbound normalizer strips it from every streaming request, so this history
    costs the provider far less input than a naive field-set estimate says.
    """
    return [
        {"role": "system", "content": "You are a careful engineer."},
        {"role": "user", "content": "Fix the failing test."},
        {
            "role": "assistant",
            "content": "Looking at the module now.",
            "reasoning_content": "step " * (reasoning_chars // 5),
        },
        {"role": "user", "content": "Continue."},
    ]


def _without_reasoning_content(messages):
    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


def test_reservation_without_reasoning_equals_reservation_of_stripped_history():
    """The streaming reservation must price the request the adapter will send.

    ``openai_provider._build_request_kwargs`` passes
    ``keep_reasoning_content=not stream``, so on a streaming call the request
    body is exactly this history minus ``reasoning_content``. Estimating that
    body is the whole fix: equality here, not a ratio, is the contract.
    """
    messages = _reasoning_heavy_messages()
    stripped = _without_reasoning_content(messages)

    kept = estimate_request_tokens(messages)
    dropped = estimate_request_tokens(messages, keep_reasoning_content=False)

    assert dropped == estimate_request_tokens(stripped)
    assert dropped == estimate_request_tokens(
        stripped, keep_reasoning_content=False
    )
    # Not a marginal correction: recorded reasoning was the bulk of the input.
    assert kept > 3 * dropped

    # Same contract for the per-message half the shaping layers call.
    assert estimate_request_message_tokens(
        messages, keep_reasoning_content=False
    ) == estimate_request_message_tokens(stripped)


def test_reservation_default_still_counts_reasoning_content():
    """Non-streaming callers resend recorded reasoning, so nothing changes.

    The keyword defaults to True precisely so every existing call site keeps
    its old number; a regression here is a silent behaviour change for the
    non-streaming baseline.
    """
    messages = _reasoning_heavy_messages(reasoning_chars=60_000)
    stripped = _without_reasoning_content(messages)

    assert estimate_request_tokens(messages) > estimate_request_tokens(stripped)
    assert estimate_request_message_tokens(
        messages
    ) > estimate_request_message_tokens(stripped)


def _streaming_agent(stream: bool):
    agent = FakeAgent()
    agent.llm_stream_chat = stream
    return agent


def test_streaming_session_is_not_stopped_by_reasoning_it_will_not_resend():
    """The astropy-14096 shape: budget above the real input, below the naive one.

    With streaming on the request carries no ``reasoning_content``, so a seat
    holding more than the stripped estimate has real headroom and the call must
    go out. Counting the reasoning here is what stopped 89 sessions before a
    single model call.
    """
    messages = _reasoning_heavy_messages()
    naive = estimate_request_tokens(messages)
    honest = estimate_request_tokens(messages, keep_reasoning_content=False)
    budget = honest + 20_000
    assert budget < naive

    llm = FakeLLM([llm_response(content="done", input_tokens=10, total_tokens=20)])
    state = SessionState(messages=list(messages))
    runner = build_runner(
        state=state,
        agent=_streaming_agent(True),
        llm=llm,
        max_budget_tokens=budget,
    )

    assert run(runner.run_loop()) == "done"
    assert len(llm.calls) == 1
    assert state.phase is not SessionPhase.STOPPED or not (
        state.terminal_reason or ""
    ).startswith("budget exhausted before model call")


def test_non_streaming_session_still_reserves_reasoning_it_will_resend():
    """Positive control for the flag: same history, streaming off, still stops.

    Without this the previous test would pass for the wrong reason (e.g. the
    reservation dropped out entirely rather than following the adapter).
    """
    messages = _reasoning_heavy_messages()
    honest = estimate_request_tokens(messages, keep_reasoning_content=False)
    budget = honest + 20_000

    llm = FakeLLM([llm_response(content="should not run")])
    state = SessionState(messages=list(messages))
    runner = build_runner(
        state=state,
        agent=_streaming_agent(False),
        llm=llm,
        max_budget_tokens=budget,
    )

    assert run(runner.run_loop()) == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("budget exhausted before model call")
