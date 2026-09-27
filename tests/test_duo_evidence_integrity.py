"""Duo keeps complete public evidence and applies deterministic fallback rules."""

import copy
import json

import pytest
from test_duo_selector import Context, candidate, decision

from opencollab.builtin_workflows import _candidate_records as records
from opencollab.builtin_workflows import _dual_coder as runner
from opencollab.builtin_workflows import _selection as selection
from opencollab.builtin_workflows import duo
from opencollab.workflows import CandidateRun


def with_records(label, test_records, diff=None):
    source = candidate(label, label)
    return CandidateRun(label=label, output={"public_test_records": test_records},
                        diff=source.diff if diff is None else diff, test_records=(), verified_targets=())


def test_long_commands_and_late_failure_remain_distinct():
    prefix = "echo public; " * 500
    record = {"target": "long-target/" * 500, "runner": "pytest", "command": prefix + "pytest test_a.py",
              "exit_code": 0, "verified": True}
    assert records._public_record(record) == record
    other = {**record, "command": prefix + "pytest test_b.py", "exit_code": 1, "verified": False}
    assert records._public_red_winner(with_records("A", [record]), with_records("B", [other])) is None
    green = [{**record, "target": f"target_{i}"} for i in range(12)]
    red = copy.deepcopy(green)
    red[-1].update(exit_code=1, verified=False)
    assert records._public_red_winner(with_records("A", red), with_records("B", green)) == "B"


async def test_large_complete_evidence_reaches_adjudicator():
    a = with_records("A", [], candidate("A", "A").diff + "+" + "detail " * 45000 + "A_FIX\n")
    b = with_records("B", [], candidate("B", "B").diff + "+" + "detail " * 45000 + "B_FIX\n")
    ctx = Context()
    winner, _, _ = await runner._contract_adjudicate(ctx, goal="Repair behavior", candidate_a=a, candidate_b=b)
    assert winner == "B"
    prompt = ctx.selector_calls[0][0]
    for source in (a, b):
        assert json.dumps(source.diff, ensure_ascii=False)[1:-1] in prompt
    encoded, paths, truncated = selection._judge_input(a, b)
    assert not truncated and json.loads(encoded)["B"]["diff"] == b.diff
    assert paths == {"A": ["src/handler.py"], "B": ["src/handler.py"]}


@pytest.mark.parametrize("diffs,expected,reason", [
    (("", ""), None, "both-empty"),
    (("a", ""), "A", "only-a-nonempty"),
    (("", "b"), "B", "only-b-nonempty"),
    (("same", "same"), "A", "identical-diff"),
])
def test_mechanical_choice_handles_empty_and_identical_candidates(diffs, expected, reason):
    a, b = (with_records(label, [], diff) for label, diff in zip("AB", diffs))
    assert selection._mechanical_choice(a, b) == (expected, reason, False)


async def test_adoption_falls_back_and_reports_actual_candidate():
    class FailingAdoption(Context):
        async def adopt_candidate(self, selected, *, preserve_paths):
            if selected.label.endswith("b"):
                raise RuntimeError("candidate unavailable")
            await super().adopt_candidate(selected, preserve_paths=preserve_paths)
    ctx = FailingAdoption()
    result = await duo(ctx, {"goal": "Repair behavior"})
    assert result["winner"] == "B" and result["adopted"] == "A"
    assert result["adoption_attempts"] == ["B", "A"]
    assert result["status"] == "done"


async def test_failed_judge_uses_a_and_candidate_mutation_blocks_adoption():
    class Unavailable(Context):
        async def agent(self, prompt, **options):
            raise RuntimeError("provider unavailable")
    result = await duo(Unavailable(), {"goal": "Repair behavior"})
    assert result["winner"] == result["adopted"] == "A"
    class Changed(Context):
        async def diff(self):
            return "changed" if self.coder_calls else "clean"
    ctx = Changed(result=decision())
    with pytest.raises(RuntimeError, match="source worktree changed"):
        await duo(ctx, {"goal": "Repair behavior"})
    assert not ctx.adoptions


@pytest.mark.parametrize("value", ["true", "false", 1, None])
async def test_local_shell_option_rejects_non_boolean_before_any_role(value):
    ctx = Context()
    with pytest.raises(ValueError, match="allow_unisolated_shell must be a boolean"):
        await duo(ctx, {"goal": "Repair behavior", "allow_unisolated_shell": value})
    assert not ctx.coder_calls
