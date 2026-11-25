"""Trajectory records identify their agent, including refused tool calls."""

from __future__ import annotations

import json

import pytest

from opencollab.adapters.trace import Tracer
from opencollab.application.event_bus import EventBus
from opencollab.application.session import Session as SessionFacade
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.bootstrap.container import build_session_runtime
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    FakeTool,
    llm_response,
    run,
    tool_call,
)


def _records(tracer: Tracer) -> list[dict]:
    tracer.flush()
    with open(tracer._path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]

def test_every_record_a_session_writes_names_its_agent(local_environment_factory, tmp_path) -> None:
    tracer = Tracer("run-1", output_dir=str(tmp_path), filename="trajectory.jsonl")
    agent = FakeAgent(tools=[FakeTool(name="known_tool")])
    agent.name = "coder"
    fake_llm = FakeLLMClient(
        [
            llm_response(
                tool_calls=[tool_call(name="missing_tool", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="recovered"),
        ]
    )
    session = Session(
        env=local_environment_factory(), agent=agent, llm=fake_llm, tracer=tracer, event_sink=EventBus(None), aid=3)
    try:
        assert run(session.run_loop()) == "recovered"
        records = _records(tracer)
    finally:
        tracer.close()

    assert records
    assert {(r["aid"], r["role"]) for r in records} == {(3, "coder")}
    refused = [r for r in records if r["type"] == "tool_error"]
    assert refused and refused[0]["run_id"] == "run-1"

def test_a_record_written_on_no_agents_behalf_says_so(tmp_path) -> None:
    tracer = Tracer("run-1", output_dir=str(tmp_path), filename="trajectory.jsonl")
    try:
        tracer.log_step("assigned.topology_edges", {"edges": []})
        (record,) = _records(tracer)
    finally:
        tracer.close()
    assert record["aid"] is None
    assert record["role"] is None

def test_the_role_is_read_when_the_record_is_written(tmp_path) -> None:
    """A prebuilt teammate is named after its session is built."""
    tracer = Tracer("run-1", output_dir=str(tmp_path), filename="trajectory.jsonl")
    agent = FakeAgent()
    agent.name = "default"
    view = tracer.for_agent(2, agent)
    agent.name = "tester"
    try:
        view.log_step("tool_exec", {})
        (record,) = _records(tracer)
    finally:
        tracer.close()
    assert (record["aid"], record["role"]) == (2, "tester")

def _one_turn_llm() -> FakeLLMClient:
    return FakeLLMClient(
        [
            llm_response(
                tool_calls=[tool_call(name="missing_tool", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="recovered"),
        ]
    )

@pytest.mark.parametrize("overrides", [
    {}, {"aid": 23}, {"run_id": "continued"}, {"aid": 0, "run_id": "continued"}, {"aid": -1},
])
def test_loaded_session_records_the_effective_identity(local_environment_factory, tmp_path, overrides) -> None:
    saved = Session(
        env=local_environment_factory(), agent=FakeAgent(), llm=_one_turn_llm(), aid=7, run_id="original",
    )
    snapshot = tmp_path / "session.json"
    saved.save(str(snapshot))

    expected_aid = overrides.get("aid", 7)
    expected_run_id = overrides.get("run_id", "original")
    automatic = tmp_path / "continued.json"
    events = []
    tracer = Tracer(expected_run_id, output_dir=str(tmp_path), filename="trajectory.jsonl")
    agent = FakeAgent(tools=[FakeTool(name="known_tool")])
    agent.name = "coder"
    try:
        session = load_session(
            str(snapshot), agent, env=local_environment_factory(),
            llm=_one_turn_llm(), tracer=tracer, event_sink=events.append,
            auto_save_path=str(automatic), **overrides,
        )
        assert (session.state.aid, session.run_id) == (expected_aid, expected_run_id)
        initial = json.loads(automatic.read_text(encoding="utf-8"))
        assert (initial["aid"], initial["run_id"]) == (expected_aid, expected_run_id)

        assert run(session.run_loop()) == "recovered"
        records = _records(tracer)
        continued = json.loads(automatic.read_text(encoding="utf-8"))
        assert (continued["aid"], continued["run_id"]) == (expected_aid, expected_run_id)
    finally:
        tracer.close()

    assert events and {event.data["aid"] for event in events} == {expected_aid}
    assert records
    assert {(r["aid"], r["role"], r["run_id"]) for r in records} == {(expected_aid, "coder", expected_run_id)}

def test_a_tracer_set_on_a_live_session_names_that_session(local_environment_factory, tmp_path) -> None:
    """Replacing ``session.tracer`` with a plain ``Tracer`` keeps attribution."""
    agent = FakeAgent(tools=[FakeTool(name="known_tool")])
    agent.name = "coder"
    session = Session(
        env=local_environment_factory(), agent=agent, llm=_one_turn_llm(), event_sink=EventBus(None), aid=3)
    tracer = Tracer("run-2", output_dir=str(tmp_path), filename="trajectory.jsonl")
    session.tracer = tracer
    try:
        assert run(session.run_loop()) == "recovered"
        records = _records(tracer)
    finally:
        tracer.close()

    assert records
    assert {(r["aid"], r["role"]) for r in records} == {(3, "coder")}

def test_restoring_into_a_built_session_moves_its_records_to_the_restored_aid(
    local_environment_factory, tmp_path,
) -> None:
    """``Session.restore`` changes ``state.aid`` after the runner exists."""
    saved = Session(
        env=local_environment_factory(), agent=FakeAgent(), llm=_one_turn_llm(), event_sink=EventBus(None), aid=7)
    snapshot = tmp_path / "session.json"
    saved.save(str(snapshot))

    tracer = Tracer("run-1", output_dir=str(tmp_path), filename="trajectory.jsonl")
    agent = FakeAgent(tools=[FakeTool(name="known_tool")])
    agent.name = "coder"
    session = Session(
        env=local_environment_factory(), agent=agent, llm=_one_turn_llm(),
        tracer=tracer, event_sink=EventBus(None), aid=3,
    )
    session.restore(str(snapshot))
    try:
        assert run(session.run_loop()) == "recovered"
        records = _records(tracer)
    finally:
        tracer.close()

    run_records = [r for r in records if r["type"] != "session.history_compaction"]
    assert run_records
    assert {(r["aid"], r["role"]) for r in run_records} == {(7, "coder")}

@pytest.mark.parametrize("tracer_argument", ["omitted", "none", "replacement"])
def test_direct_runtime_session_preserves_tracing(local_environment_factory, tmp_path, tracer_argument) -> None:
    original = Tracer("original", output_dir=str(tmp_path))
    replacement = Tracer("replacement", output_dir=str(tmp_path))
    agent = FakeAgent(tools=[FakeTool(name="known_tool")])
    agent.name = "coder"
    try:
        runtime = build_session_runtime(
            env=local_environment_factory(),
            agent=agent, llm=_one_turn_llm(), tracer=original, event_sink=EventBus(None), aid=7,
        )
        kwargs = {} if tracer_argument == "omitted" else {
            "tracer": replacement if tracer_argument == "replacement" else None,
        }
        session = SessionFacade(agent=agent, runtime=runtime, **kwargs)
        selected = replacement if tracer_argument == "replacement" else original
        initial_count = len(_records(selected))
        assert run(session.run_loop()) == "recovered"
        emitted = _records(selected)[initial_count:]
        assert {"llm_call", "tool_error"} <= {record["type"] for record in emitted}
        assert {(record["aid"], record["role"]) for record in emitted} == {(7, "coder")}
        assert session.runner.tracer is session.tracer
        assert session.tool_execution.tracer is session.tracer
        if tracer_argument == "replacement":
            assert len(_records(original)) == 1

        snapshot = tmp_path / "session.json"
        saved = Session(env=local_environment_factory(), agent=agent, llm=_one_turn_llm(), aid=9)
        saved.save(str(snapshot))
        session.restore(str(snapshot))
        session.agent.name = "reviewer"
        session.tracer.log_step("after-restore", {})
        assert (_records(selected)[-1]["aid"], _records(selected)[-1]["role"]) == (9, "reviewer")

        session.tracer = None
        assert session.runner.tracer is None
        assert session.tool_execution.tracer is None
    finally:
        original.close()
        replacement.close()
