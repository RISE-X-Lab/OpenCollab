import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.application.scheduler import Scheduler
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport


@pytest.mark.parametrize("timeout", [5.0, None], ids=["deadline", "unbounded"])
async def test_team_propagates_cancellation_after_scheduler_completion(tmp_path, monkeypatch, timeout):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(ledger))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    config = tmp_path / "team.yaml"
    config.write_text(
        "entry: lead\nroles:\n  lead:\n    model: gpt-4o\n"
        "    prompt: Report the verified total.\n    tools: []\n",
        encoding="utf-8",
    )
    answer = "The verified total is 17."

    async def model_response(request):
        return completion_http_response(request, text=answer)

    install_sdk_transport(monkeypatch, "openai", model_response)
    app = OpenCollab(
        tmp_path, model="gpt-4o", provider="openai", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0},
    )
    original_run = Scheduler.run
    cancelled = []

    def cancel_caller(_owner=None):
        cancelled.append(caller.cancel("caller stopped before delivery"))

    async def cancel_after_completion(self, prompt):
        output = await original_run(self, prompt)
        owner = asyncio.current_task()
        if timeout is None:
            assert owner is caller
        if owner is caller:
            asyncio.get_running_loop().call_soon(cancel_caller)
        else:
            owner.add_done_callback(cancel_caller)
        return output

    monkeypatch.setattr(Scheduler, "run", cancel_after_completion)
    artifacts = tmp_path / "artifacts"
    cancellation = []

    async def run_team():
        try:
            return await app.team(
                "Report the total.", config=config, timeout=timeout, artifacts=artifacts,
                budget=20_000, use_worktrees=False,
            )
        except asyncio.CancelledError as exc:
            cancellation.append(exc.args)
            raise

    caller = asyncio.create_task(run_team())
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert cancelled == [True]
    assert cancellation == [("caller stopped before delivery",)]
    snapshot = app.read_session_snapshot(next(artifacts.glob("agent_0_*.json")))
    assert snapshot["messages"][-1]["content"] == answer
    assert snapshot["session_state"]["phase"] == "done"
    assert snapshot["session_state"]["used_tokens"] == 4
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [record["usage"]["total_tokens"] for record in records] == [4]
