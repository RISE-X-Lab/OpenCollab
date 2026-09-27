from types import SimpleNamespace

from opencollab.application.session_run import SessionRunUseCase


def test_unbounded_session_still_emits_terminal_evidence():
    events = []
    session = SimpleNamespace(
        _session_terminal_traced=False,
        tracer=SimpleNamespace(log_step=lambda **event: events.append(event)),
        state=SimpleNamespace(step_count=13, aid=1,
                              phase=SimpleNamespace(value="stopped"),
                              terminal_reason="finished", used_tokens=1234),
        max_steps=None,
        max_budget_tokens=None,
        agent=SimpleNamespace(name="candidate-a"),
    )
    SessionRunUseCase._trace_session_terminal(session)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["step_count"] == 13
    assert payload["max_steps"] is None
    assert payload["step_ceiling_reached"] is False
    assert payload["max_budget_tokens"] is None
    assert payload["used_tokens"] == 1234
