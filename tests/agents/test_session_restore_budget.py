"""Restored threshold cadence follows the snapshot in fresh and reused Sessions."""

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from tests.runtime.test_llm_chat_streaming import FakeClient
from tests.runtime.test_llm_chat_streaming_session import completion


async def make_session(tmp_path, *spends):
    responses = []
    for spend in spends:
        response = completion("done", [], "stop")
        response.usage.prompt_tokens = spend - 10
        response.usage.completion_tokens = 10
        response.usage.total_tokens = spend
        responses.append(response)
    wire = FakeClient(responses)
    client = LLMClient(model="gpt-4o", api_key="unused", max_retries=0)
    await client._openai.close()
    client._openai = wire
    session = build_session(
        agent=Agent(name="coder", system_prompt="system", model="gpt-4o"),
        llm=client, env=LocalEnvironment(str(tmp_path)), max_budget_tokens=100_000, max_steps=None,
    )
    return session, wire


async def test_restore_reused_session_matches_fresh_threshold_cadence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "thresholds")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    source, _ = await make_session(tmp_path, 25_000)
    await source.add_user_message("source turn")
    assert await source.run_loop() == "done"
    snapshot = tmp_path / "restore.json"
    source.save(str(snapshot))

    reused, reused_wire = await make_session(tmp_path, 200, 25_000, 25_000)
    await reused.add_user_message("earlier turn")
    assert await reused.run_loop() == "done"
    fresh, fresh_wire = await make_session(tmp_path, 25_000, 25_000)

    for session in (reused, fresh):
        session.restore(str(snapshot))
        assert session.used_tokens == 25_000
        await session.add_user_message("restored turn")
        assert await session.run_loop() == "done"
    reused_messages = reused_wire.calls[1]["messages"]
    fresh_messages = fresh_wire.calls[0]["messages"]
    assert reused_messages == fresh_messages
    assert reused_messages[-1]["content"] == "restored turn"

    # New spending after restore crosses the next band and re-arms the nudge.
    for session in (reused, fresh):
        await session.add_user_message("following turn")
        assert await session.run_loop() == "done"
    assert reused_wire.calls[2]["messages"] == fresh_wire.calls[1]["messages"]
    assert "[Budget:" in reused_wire.calls[2]["messages"][-1]["content"]
    assert reused.used_tokens == fresh.used_tokens == 75_000
