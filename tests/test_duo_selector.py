"""Exercise Duo selection and per-call selector isolation."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from opencollab.builtin_workflows import _dual_coder as g21
from opencollab.workflows import CandidateRun

g22 = importlib.import_module("opencollab.builtin_workflows.duo")

WORKFLOW = "validation-council-dual-coder-selection-v2"


def candidate(label, value):
    return CandidateRun(
        label=label,
        output="Public repair completed",
        diff=("diff --git a/src/handler.py b/src/handler.py\n"
              "--- a/src/handler.py\n+++ b/src/handler.py\n"
              f"@@ -1 +1 @@\n-old\n+{value}\n"),
        test_records=(),
        verified_targets=(),
    )


def decision(evidence="src/handler.py implements the required return value"):
    return {
        "winner": "B",
        "requirements_complete": True,
        "requirements": [{
            "requirement": "Preserve the public return value",
            "a_coverage": "not_covered",
            "b_coverage": "covered",
            "a_evidence": ["src/handler.py retains the incorrect value"],
            "b_evidence": [evidence],
        }],
        "rationale": "B covers the requirement that A leaves unresolved",
    }


class Context:
    def __init__(self, *, result=None, barrier=None, identical=False):
        self.result = result or decision()
        self.barrier = barrier
        self.identical = identical
        self.coder_calls = []
        self.selector_calls = []
        self.adoptions = []
        self.phases = []

    async def candidate_agent(self, prompt, **options):
        if not self.coder_calls and self.barrier is not None:
            await self.barrier()
        self.coder_calls.append((prompt, options))
        value = "a" if self.identical or len(self.coder_calls) == 1 else "b"
        return candidate(options["label"], value)

    async def agent(self, prompt, **options):
        self.selector_calls.append((prompt, options))
        return self.result

    async def phase(self, title):
        self.phases.append(title)

    async def diff(self):
        return "[Working tree status]\n(clean)"

    async def adopt_candidate(self, selected, *, preserve_paths):
        self.adoptions.append((selected, preserve_paths))

    async def log(self, message):
        pass

    def tokens_spent(self):
        return 0




@pytest.mark.asyncio
async def test_parallel_g21_g22_keep_their_selector_prompts_and_role_options(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    ready = asyncio.Event()
    entered = 0

    async def barrier():
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()

    original = g21.CONTRACT_PROMPT
    ctx21, ctx22 = Context(barrier=barrier), Context(barrier=barrier)
    inputs = {"description": "Preserve the public return value", "injected_test_paths": ["test_hidden.py"]}
    result22, result21 = await asyncio.wait_for(asyncio.gather(
        g22.duo(ctx22, inputs),
        g21.run_dual_coder(ctx21, inputs),
    ), timeout=2)
    assert g21.CONTRACT_PROMPT == original
    assert result21 == result22
    assert result22["winner"] == "B" and result22["adopted"] == "B"
    assert ctx21.coder_calls[0][0] == ctx22.coder_calls[0][0]
    assert ctx21.coder_calls[1][0] == ctx22.coder_calls[1][0]
    for (_, options21), (_, options22) in zip(ctx21.coder_calls, ctx22.coder_calls, strict=True):
        assert {k: v for k, v in options21.items() if k != "tools"} == {
            k: v for k, v in options22.items() if k != "tools"}
        assert [tool.name for tool in options21["tools"]] == [tool.name for tool in options22["tools"]]
        assert options21["budget"] is None
    prompt21, options21 = ctx21.selector_calls[0]
    prompt22, options22 = ctx22.selector_calls[0]
    assert prompt22.startswith(prompt21)
    assert "that entry's own a_evidence and b_evidence arrays" in prompt22
    assert "that entry's own a_evidence and b_evidence arrays" not in prompt21
    assert options21 == options22
    assert options22["schema"] is g21.contract.CONTRACT_SCHEMA
    assert options22["tools"] == [] and options22["budget"] is None
    assert ctx21.adoptions[0][1] == ctx22.adoptions[0][1] == ["test_hidden.py"]
    assert ctx21.phases == ctx22.phases


@pytest.mark.asyncio
async def test_g22_preserves_original_rejection_and_default_a(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    result = decision("The candidate implements the requirement")
    ctx = Context(result=result)
    outcome = await g22.duo(
        ctx, {"goal": "Preserve the public return value"},
    )
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_g22_identical_candidates_keep_mechanical_selection():
    ctx = Context(identical=True)
    outcome = await g22.duo(ctx, {"goal": "Repair public behavior"})
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "identical-diff"
    assert outcome["judge_used"] is False and not ctx.selector_calls
