from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.sdk import BudgetDecision


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")


def user_label(messages):
    return next(row["content"] for row in reversed(messages) if row["role"] == "user")


async def wait(event):
    await asyncio.wait_for(event.wait(), 4)


def brief(row):
    return row.status, row.output, row.hard_budget_tokens, row.reason


@pytest.mark.parametrize("first_cost,overlap", [(100, False), (650, True), (100, True)])
async def test_later_review_can_spend_immediately_free_tokens(tmp_path, first_cost, overlap):
    preparing, release_prepare = asyncio.Event(), asyncio.Event()
    entered, start_first, first_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    outsider_active, release_outsider, start_review = (asyncio.Event() for _ in range(3))
    estimates = {"prepare": 590, "intro": first_cost, "outside": 90, "review": 3000}
    calls, rows, views = [], {}, {}

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return estimates[user_label(messages)]

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            name = user_label(messages)
            calls.append((name, max_output_tokens))
            if name == "prepare":
                preparing.set()
                await wait(release_prepare)
            elif name == "outside":
                outsider_active.set()
                await wait(release_outsider)
            return LLMResponse(content=name, usage=Usage(estimates[name], 10), finish_reason="stop")

    async def flow(ctx, _inputs):
        async def intro():
            entered.set()
            await wait(start_first)
            views["first_entry"] = ctx.tokens_remaining()
            row = await ctx.agent_run("intro", tools=[])
            rows["intro"] = brief(row)
            first_done.set()
            return row.output

        async def review():
            await wait(start_review)
            views["review_entry"] = ctx.tokens_remaining()
            row = await ctx.agent_run("review", tools=[])
            rows["review"] = brief(row)
            return row.output

        outs = None
        collection = None
        preparer = asyncio.create_task(ctx.agent_run("prepare", budget=19000, tools=[]))
        try:
            await wait(preparing)
            views["collection_entry"] = ctx.tokens_remaining()
            collection = asyncio.create_task(ctx.parallel([intro, review]))
            await wait(entered)
            release_prepare.set()
            await preparer
            start_first.set()
            await wait(first_done)
            if overlap:
                outs = asyncio.create_task(ctx.agent_run("outside", budget=15000, tools=[]))
                await wait(outsider_active)
            start_review.set()
            output = await collection
            if outs is not None:
                release_outsider.set()
                await outs
            views["lease_count"] = len(ctx.budget._leases)
            views["remaining"] = ctx.tokens_remaining()
            return output
        finally:
            for event in (release_prepare, start_first, start_review, release_outsider):
                event.set()
            await asyncio.gather(*[t for t in (preparer, collection, outs) if t], return_exceptions=True)

    result = await OpenCollab(tmp_path, model="offline", config={"max_output_tokens": 128}).workflow(
        flow, llm=Model(), budget=20000, concurrency=3, task_concurrency=2,
        limit_mode="explicit", timeout=10, trace=False,
    )
    assert result.status == "completed", result.reason
    assert views["collection_entry"] == 1000
    assert views["first_entry"] == 19400
    assert views["review_entry"] >= estimates["review"] + 128
    assert views["lease_count"] == 0
    assert views["remaining"] == 20000 - result.tokens
    assert result.output == ["intro", "review"]
    assert calls[-1] == ("review", 128)


@pytest.mark.parametrize(
    "review_input,prepare_tokens,configured_output,expected_output,expected_limits,review_tokens",
    [
        (5750, 3000, 128, "Review complete", [128], 5774),
        (5990, 3000, 128, "Review complete", [10, 128], 12014),
        (5990, 3000, 8192, "Review complete", [10, 1510], 12014),
        (5990, 10000, 128, None, [10], 6000),
    ],
)
async def test_truncated_request_replays_only_with_full_budget(
    tmp_path, review_input, prepare_tokens, configured_output, expected_output, expected_limits, review_tokens,
):
    preparing, release_prepare = asyncio.Event(), asyncio.Event()
    entered, start = asyncio.Event(), asyncio.Event()
    calls = []

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return 2990 if user_label(messages) == "prepare" else review_input

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            name = user_label(messages)
            calls.append((name, max_output_tokens))
            if name == "prepare":
                preparing.set()
                await wait(release_prepare)
                return LLMResponse(content="prepared", usage=Usage(prepare_tokens - 10, 10), finish_reason="stop")
            if max_output_tokens < 24:
                return LLMResponse(
                    content="Partial review", usage=Usage(review_input, max_output_tokens), finish_reason="length",
                )
            return LLMResponse(content="Review complete", usage=Usage(review_input, 24), finish_reason="stop")

    async def flow(ctx, _inputs):
        async def review():
            entered.set()
            await wait(start)
            return brief(await ctx.agent_run("review", tools=[]))

        async def record():
            return "recorded"

        prep = asyncio.create_task(ctx.agent_run("prepare", budget=18000, tools=[]))
        group = None
        try:
            await wait(preparing)
            assert ctx.tokens_remaining() == 12000
            group = asyncio.create_task(ctx.parallel([review, record]))
            await wait(entered)
            release_prepare.set()
            await prep
            assert ctx.tokens_remaining() == 30000 - prepare_tokens
            start.set()
            return await group
        finally:
            release_prepare.set()
            start.set()
            await asyncio.gather(*[t for t in (prep, group) if t], return_exceptions=True)

    result = await OpenCollab(tmp_path, model="offline", config={"max_output_tokens": configured_output}).workflow(
        flow, llm=Model(), budget=30000, concurrency=2, task_concurrency=2,
        limit_mode="explicit", timeout=10, trace=False,
    )
    assert result.status == "completed", result.reason
    row_status, output = result.output[0][:2]
    if expected_output is None:
        assert row_status == "stopped"
        assert "output truncated" in result.output[0][3]
    else:
        assert (row_status, output) == ("completed", expected_output)
    assert calls == [("prepare", configured_output), *(("review", limit) for limit in expected_limits)]
    assert result.tokens == prepare_tokens + review_tokens


@pytest.mark.parametrize(
    "control_kind,budget,expected",
    [
        ("none", None, "completed"),
        ("observer", None, "completed"),
        ("soft", None, "stopped"),
        ("policy", None, "stopped"),
        ("none", 750, "stopped"),
    ],
)
async def test_recovery_respects_session_authority(tmp_path, control_kind, budget, expected):
    preparing, release_prepare, collection_entered, start = [asyncio.Event() for _ in range(4)]
    events, snapshots, calls = [], [], []

    def decide(snapshot):
        snapshots.append(snapshot)
        return BudgetDecision(snapshot.soft_budget_tokens)

    control = {
        "none": None,
        "observer": RunControl(on_event=events.append),
        "soft": RunControl(initial_soft_budget_tokens=400, on_event=events.append),
        "policy": RunControl(decide_budget=decide, on_event=events.append),
    }[control_kind]

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return {"prepare": 590, "review": 800}[user_label(messages)]

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            name = user_label(messages)
            calls.append((name, max_output_tokens))
            if name == "prepare":
                preparing.set()
                await wait(release_prepare)
                return LLMResponse(content=name, usage=Usage(590, 10), finish_reason="stop")
            return LLMResponse(content=name, usage=Usage(800, 10), finish_reason="stop")

    async def flow(ctx, _inputs):
        async def review():
            collection_entered.set()
            await wait(start)
            return await ctx.agent_run("review", budget=budget, tools=[], run_control=control)

        async def note():
            return "noted"

        prep = asyncio.create_task(ctx.agent_run("prepare", budget=19000, tools=[]))
        group = None
        try:
            await wait(preparing)
            group = asyncio.create_task(ctx.parallel([review, note]))
            await wait(collection_entered)
            release_prepare.set()
            await prep
            start.set()
            return await group
        finally:
            release_prepare.set()
            start.set()
            await asyncio.gather(*[t for t in (prep, group) if t], return_exceptions=True)

    result = await OpenCollab(tmp_path, model="offline", config={"max_output_tokens": 128}).workflow(
        flow, llm=Model(), budget=20000, concurrency=2, task_concurrency=2,
        limit_mode="explicit", timeout=10, trace=False,
    )
    row = result.output[0]
    assert result.status == "completed", result.reason
    assert row.status == expected
    if expected == "completed":
        assert row.output == "review"
        assert row.hard_budget_tokens == row.soft_budget_tokens == 928
        assert calls[-1] == ("review", 128)
    else:
        assert calls == [("prepare", 128)]
        assert row.hard_budget_tokens == (budget or 500)
        assert row.soft_budget_tokens == (400 if control_kind == "soft" else row.hard_budget_tokens)
    if control_kind == "observer":
        assert sum(event.type == "usage" for event in events) == 1
        assert sum(event.type == "cleanup_completed" for event in events) == 1
    if control_kind == "policy":
        assert snapshots
        assert {snap.hard_budget_tokens for snap in snapshots} == {500}


@pytest.mark.parametrize(
    "prepare_cap,commit_reserve", [(18_000, 256), (19_800, 256), (19_800, 100)],
)
async def test_released_budget_reaches_enforced_scout_before_wind_down(tmp_path, prepare_cap, commit_reserve):
    (tmp_path / "notes.txt").write_text("Observed measurement is 42 ms.\n")
    preparing, release = asyncio.Event(), asyncio.Event()
    entered, start = asyncio.Event(), asyncio.Event()
    actions = []

    async def wait(event):
        await asyncio.wait_for(event.wait(), 5)

    def call(name, arguments):
        return {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return 80

        async def complete(self, messages, *, tools, **kwargs):
            user = next(row["content"] for row in messages if row["role"] == "user")
            if user == "prepare":
                preparing.set()
                await wait(release)
                return LLMResponse(content="prepared", usage=Usage(590, 10), finish_reason="stop")
            tool_names = [tool["function"]["name"] for tool in tools]
            evidence = any(
                row["role"] == "tool" and "Observed measurement is 42 ms." in row["content"] for row in messages
            )
            if "file_read" in tool_names and not evidence:
                actions.append("read")
                response = call("file_read", {"path": "notes.txt"})
            elif evidence:
                actions.append("submit")
                response = call("submit_findings", {
                    "summary": "Measured time was 42 ms.",
                    "findings": [{
                        "aspect": "runtime", "claim": "Time was 42 ms", "evidence_anchor": "notes.txt:1",
                        "verified": True, "confidence": "high",
                    }],
                    "insufficient_evidence": False,
                })
            else:
                actions.append("premature-submit")
                response = call("submit_findings", {
                    "summary": "No evidence was collected.", "findings": [], "insufficient_evidence": True,
                })
            return LLMResponse(tool_calls=[response], usage=Usage(80, 15), finish_reason="tool_calls")

    async def flow(ctx, _inputs):
        async def scout():
            entered.set()
            await wait(start)
            return await ctx.agent(
                "Measure the saved time.", tools=[FileReadTool()], label="scout",
                enforcement_strength=ENFORCEMENT_ON, commit_reserve=commit_reserve,
            )

        async def note():
            return "recorded"

        prep = asyncio.create_task(ctx.agent_run("prepare", budget=prepare_cap, tools=[]))
        group = None
        try:
            await wait(preparing)
            assert ctx.tokens_remaining() == 20_000 - prepare_cap
            group = asyncio.create_task(ctx.parallel([scout, note]))
            await wait(entered)
            release.set()
            await prep
            assert ctx.tokens_remaining() == 19_400
            start.set()
            return await group
        finally:
            release.set()
            start.set()
            await asyncio.gather(*[t for t in (prep, group) if t], return_exceptions=True)

    result = await OpenCollab(tmp_path, model="offline", config={"max_output_tokens": 128}).workflow(
        flow, llm=Model(), budget=20000, concurrency=2, task_concurrency=2,
        limit_mode="explicit", timeout=10, trace=False,
    )

    assert result.status == "completed", result.reason
    assert actions == ["read", "submit"]
    assert "Measured time was 42 ms." in result.output[0]
