from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.submit import SubmitTool
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import FakeLLMClient, llm_response, tool_call


@pytest.mark.parametrize("expected", ["pear", ""])
async def test_final_step_autosave_restores_the_completed_answer(tmp_path, expected):
    path = tmp_path / "automatic.json"
    agent = Agent(name="assistant", system_prompt="Answer the current question.", tools=[])
    final_replies = [expected] if expected else [None, None]
    model = FakeLLMClient([llm_response(content=text) for text in ["apple", *final_replies]])
    source = build_session(agent=agent, llm=model, env=LocalEnvironment(str(tmp_path)), auto_save_path=str(path))
    saved, release = asyncio.Event(), asyncio.Event()

    async def hold_final_step(event):
        if event.type == "step_end" and event.data["step"] == 1 + len(final_replies):
            await source.enqueue_auto_save()
            saved.set()
            await release.wait()

    source.event_bus.subscribe(hold_final_step)
    restored = running = None
    try:
        await source.add_user_message("First question")
        assert await source.run_loop() == "apple"
        await source.add_user_message("Second question")
        running = asyncio.create_task(source.run_loop())
        await asyncio.wait_for(saved.wait(), timeout=3)

        snapshot = OpenCollab.read_session_snapshot(path)
        assert snapshot["session_state"]["phase"] == "done"
        assert snapshot["session_state"]["terminal_reason"] == "completed"
        assert snapshot["session_state"]["active_turn_start_message_index"] is None
        resume_model = FakeLLMClient()
        restored = load_session(str(path), agent=agent, llm=resume_model, env=LocalEnvironment(str(tmp_path)))
        assert await restored.run_loop() == expected
        assert restored.used_tokens == source.used_tokens
        assert resume_model.calls == []

        release.set()
        assert await running == expected
    finally:
        release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        for session in (source, restored):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


@pytest.mark.parametrize("second_texts, expected", [
    ([None, None], ""),
    ([None, "pear"], "pear"),
    (["pear"], "pear"),
])
async def test_completed_turn_answer_survives_reread_and_snapshot(tmp_path, second_texts, expected):
    automatic = tmp_path / "automatic.json"
    exported = tmp_path / "exported.json"
    agent = Agent(name="assistant", system_prompt="Answer the current question.", tools=[])
    model = FakeLLMClient([llm_response(content=text) for text in ["apple", *second_texts]])
    session = build_session(
        agent=agent, llm=model, env=LocalEnvironment(str(tmp_path)), auto_save_path=str(automatic),
    )
    copies = []
    try:
        await session.add_user_message("First question")
        assert await session.run_loop() == "apple"
        await session.add_user_message("Second question")
        assert await session.run_loop() == expected
        assert session.phase is SessionPhase.DONE
        assert session.state.active_turn_start_message_index is None
        session.save(str(exported))
        restored_models = []
        for path in [exported, automatic]:
            restored_model = FakeLLMClient([llm_response(content="third answer")])
            restored_models.append(restored_model)
            copies.append(load_session(
                str(path), agent=agent, llm=restored_model, env=LocalEnvironment(str(tmp_path)),
            ))

        assert await session.run_loop() == expected
        for restored in copies:
            assert await restored.run_loop() == expected
        assert len(model.calls) == 1 + len(second_texts)
        assert all(model.calls == [] for model in restored_models)

        restored = copies[-1]
        await restored.add_user_message("Third question")
        assert await restored.run_loop() == "third answer"
        assert await restored.run_loop() == "third answer"
        assert len(restored_models[-1].calls) == 1
    finally:
        for restored in copies:
            await restored.aclose()
            await restored.env.cleanup()
        await session.aclose()
        await session.env.cleanup()


async def test_completed_submission_keeps_priority_over_assistant_text(tmp_path):
    agent = Agent(name="assistant", system_prompt="Submit the final answer.", tools=[SubmitTool()])
    model = FakeLLMClient([
        llm_response(content="apple"),
        llm_response(content="draft pear", tool_calls=[tool_call(
            "submission", "submit", '{"summary": "accepted pear"}',
        )]),
    ])
    session = build_session(agent=agent, llm=model, env=LocalEnvironment(str(tmp_path)))
    restored = None
    try:
        await session.add_user_message("First question")
        assert await session.run_loop() == "apple"
        await session.add_user_message("Second question")
        assert await session.run_loop() == "accepted pear"
        path = tmp_path / "submitted.json"
        session.save(str(path))
        restored_model = FakeLLMClient()
        restored = load_session(
            str(path), agent=agent, llm=restored_model, env=LocalEnvironment(str(tmp_path)),
        )
        assert await session.run_loop() == "accepted pear"
        assert await restored.run_loop() == "accepted pear"
        assert len(model.calls) == 2
        assert restored_model.calls == []
    finally:
        if restored is not None:
            await restored.aclose()
            await restored.env.cleanup()
        await session.aclose()
        await session.env.cleanup()


@pytest.mark.parametrize("cursor", ["missing", None, True, -1, 10_000, "3"])
async def test_legacy_completed_snapshot_keeps_answer_query(tmp_path, cursor):
    agent = Agent(name="assistant", system_prompt="Answer the question.", tools=[])
    source = build_session(agent=agent, llm=FakeLLMClient(), env=LocalEnvironment(str(tmp_path)))
    restored = None
    try:
        source.messages = [
            {"role": "system", "content": "Answer the question."},
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "legacy answer"},
        ]
        source.state.mark_done()
        path = tmp_path / "legacy.json"
        source.save(str(path))
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if cursor == "missing":
            snapshot["session_state"].pop("completed_turn_start_message_index", None)
        else:
            snapshot["session_state"]["completed_turn_start_message_index"] = cursor
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        model = FakeLLMClient()
        restored = load_session(
            str(path), agent=agent, llm=model, env=LocalEnvironment(str(tmp_path)),
        )
        assert await restored.run_loop() == "legacy answer"
        assert model.calls == []
    finally:
        if restored is not None:
            await restored.aclose()
            await restored.env.cleanup()
        await source.aclose()
        await source.env.cleanup()
