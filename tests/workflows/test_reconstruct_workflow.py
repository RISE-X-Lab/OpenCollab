"""The reconstruct workflow: batches, promotions, early stop, gates and task resolution (no model, no Docker)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from opencollab.builtin_workflows import ReconstructionTask, get_builtin_workflows, reconstruct, register_task
from opencollab.builtin_workflows.reconstruct import (
    INITIAL_BATCH,
    MEMORISATION_PATIENCE,
    PROMOTION_EARLY_STOP,
    resolve_task,
)
from opencollab.builtin_workflows.reconstruct import _coverage as coverage
from opencollab.builtin_workflows.reconstruct import _verification as verification

CORRECT = "def solve(xs):\n    return sum(abs(x) for x in xs)\n"
LOOKUP = (
    "def solve(xs):\n"
    "    table = {(1, -3): 4, (2, -3): 5, (3, -3): 6, (4, -3): 7}\n"
    "    if tuple(xs) in table: return table[tuple(xs)]\n"
    "    if xs == [1, -3]: return 4\n"
    "    if xs == [2, -3]: return 5\n"
    "    if xs == [3, -3]: return 6\n"
    "    return 0\n"
)
# Passes every example the scripted proposer produces first, but spells the
# observed inputs out as literals: a memorising pass, not evidence of the rule.
MEMORISING = (
    "def solve(xs):\n"
    "    known = [[2, -3], [3, -3], [4, -3], [5, -3]]\n"
    "    if xs in known:\n"
    "        return sum(abs(x) for x in xs)\n"
    "    return sum(abs(x) for x in xs)\n"
)


def _oracle(xs: list[int]) -> int:
    return sum(abs(x) for x in xs)


class _Generator:
    """A deterministic stand-in for a benchmark's input generator."""

    def __init__(self) -> None:
        self._next = 100

    def __call__(self, k: int, exclude: set[str]) -> list[Any]:
        out = []
        while len(out) < k:
            self._next += 1
            inp = [self._next, -self._next, 0]
            if repr(inp) not in exclude:
                out.append(inp)
        return out


async def _evaluate(program: Path, inputs: list[Any]) -> list[dict[str, Any]]:
    namespace: dict[str, Any] = {}
    exec(program.read_text(encoding="utf-8"), namespace)  # noqa: S102 - test-authored candidates only
    results = []
    for inp in inputs:
        try:
            results.append({"ok": True, "output": namespace["solve"](inp)})
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return results


@pytest.mark.parametrize("returned_count", [0, 3])
def test_candidate_evaluator_must_cover_every_observed_input(
    tmp_path: Path, returned_count: int
) -> None:
    async def incomplete_evaluation(_program: Path, inputs: list[Any]) -> list[dict[str, Any]]:
        return [{"ok": True, "output": 0} for _ in range(returned_count)]

    task = make_task(tmp_path, evaluate=incomplete_evaluation)
    ctx = ScriptedContext(tmp_path, programs=["def solve(xs):\n    return -12345\n"])

    with pytest.raises(RuntimeError, match=rf"returned {returned_count} results for 2 inputs"):
        _run(task, ctx, max_iterations=1, early_stop=1)

    assert (tmp_path / task.candidate_file).is_file()


def test_missing_candidate_reports_no_examples_passed(tmp_path: Path) -> None:
    class NoCandidateContext(ScriptedContext):
        async def agent(self, prompt: str, *, schema=None, **kwargs):
            if schema is not None:
                return await super().agent(prompt, schema=schema, **kwargs)
            return "done"

    result = _run(make_task(tmp_path), NoCandidateContext(tmp_path), max_iterations=1)

    assert result["known_examples"] == INITIAL_BATCH
    assert result["events"][-1]["passed"] == 0
    assert result["candidate_exists"] is False
    assert result["final_score"] is None


@pytest.mark.parametrize("random_value", [(2, -3), [2, -3]])
def test_random_tuple_and_agent_json_list_count_as_one_input(
    tmp_path: Path, random_value: tuple[int, int] | list[int]
) -> None:
    class DuplicateProposalContext(ScriptedContext):
        def __init__(self, workspace: Path) -> None:
            super().__init__(workspace)
            self.proposals = 0

        async def agent(self, prompt: str, *, schema=None, **kwargs):
            if schema is not None:
                self.proposals += 1
                return {"inputs": [[2, -3] if self.proposals == 1 else [4, -3]]}
            return await super().agent(prompt, schema=schema, **kwargs)

    task = make_task(tmp_path, random_inputs=lambda k, exclude: [random_value])
    ctx = DuplicateProposalContext(tmp_path)
    result = _run(task, ctx, initial_batch=2, random_share=0.5, max_iterations=1, early_stop=1)

    assert ctx.proposals == 2
    assert result["known_examples"] == 2
    assert [row[0] for row in result["examples"]] == [random_value, [4, -3]]
    assert any("repeat" in event.get("rejected", "") for event in result["events"])


def test_random_mapping_and_reordered_agent_mapping_count_as_one_input(tmp_path: Path) -> None:
    class MappingContext(ScriptedContext):
        def __init__(self, workspace: Path) -> None:
            super().__init__(workspace, programs=["def solve(data):\n    return sum(data.values())\n"])
            self.proposals = 0

        async def agent(self, prompt: str, *, schema=None, **kwargs):
            if schema is not None:
                self.proposals += 1
                return {"inputs": [{"y": 3, "x": 2} if self.proposals == 1 else {"x": 4, "y": 3}]}
            return await super().agent(prompt, schema=schema, **kwargs)

    async def evaluate(_program: Path, inputs: list[Any]) -> list[dict[str, Any]]:
        return [{"ok": True, "output": sum(value.values())} for value in inputs]

    task = make_task(
        tmp_path, input_type="mapping", kind="other", oracle=lambda value: sum(value.values()),
        evaluate=evaluate, random_inputs=lambda k, exclude: [{"x": 2, "y": 3}],
    )
    ctx = MappingContext(tmp_path)
    result = _run(task, ctx, initial_batch=2, random_share=0.5, max_iterations=1, early_stop=1)

    assert ctx.proposals == 2
    assert result["known_examples"] == 2
    assert [row[0] for row in result["examples"]] == [{"x": 2, "y": 3}, {"x": 4, "y": 3}]
    assert any("repeat" in event.get("rejected", "") for event in result["events"])


def test_random_generator_equivalent_mapping_is_replaced_by_new_proposals(tmp_path: Path) -> None:
    class MappingContext(ScriptedContext):
        def __init__(self, workspace: Path) -> None:
            super().__init__(workspace, programs=["def solve(data):\n    return sum(data.values())\n"])
            self.requested: list[int] = []

        async def agent(self, prompt: str, *, schema=None, **kwargs):
            if schema is not None:
                n = int(prompt.split("exactly ")[1].split()[0])
                self.requested.append(n)
                values = ([{"x": 1, "y": 2}] if len(self.requested) == 1 else [
                    {"x": 3, "y": 4}, {"x": 4, "y": 5}, {"x": 5, "y": 6},
                ])
                return {"inputs": values[:n]}
            return await super().agent(prompt, schema=schema, **kwargs)

    class MappingGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, count: int, exclude: set[str]) -> list[dict[str, int]]:
            self.calls += 1
            values = ([{"x": 0, "y": 0}] if self.calls == 1 else [
                {"y": 2, "x": 1}, {"x": 7, "y": 8},
            ])
            assert len(values) == count and all(repr(value) not in exclude for value in values)
            return values

    async def evaluate(_program: Path, inputs: list[Any]) -> list[dict[str, Any]]:
        return [{"ok": True, "output": sum(value.values())} for value in inputs]

    task = make_task(
        tmp_path, input_type="mapping", kind="other", oracle=lambda value: sum(value.values()),
        evaluate=evaluate, random_inputs=MappingGenerator(),
    )
    ctx = MappingContext(tmp_path)
    result = _run(task, ctx, initial_batch=2, batch_step=2, max_iterations=2,
                  early_stop=2, random_share=0.5)

    keys = [json.dumps(row[0], sort_keys=True) for row in result["examples"]]
    assert ctx.requested == [1, 3]
    assert result["known_examples"] == len(set(keys)) == 6
    assert result["random_inputs"] == 2


def make_task(workspace: Path, **overrides: Any) -> ReconstructionTask:
    fields: dict[str, Any] = dict(
        input_type="list of integers",
        input_constraints="Valid input is a list of integers.",
        function_name="solve",
        workspace=workspace,
        oracle=_oracle,
        evaluate=_evaluate,
        random_inputs=_Generator(),
        kind="list",
        lo=-1000,
        hi=1000,
    )
    fields.update(overrides)
    return ReconstructionTask(**fields)


class ScriptedContext:
    """Plays the agent: writes the scripted candidate, proposes fresh inputs, spends no tokens."""

    def __init__(self, workspace: Path, programs: list[str] | None = None, repeat_first_proposal: bool = False):
        self.workspace = workspace
        self.programs = list(programs or [CORRECT])
        self.repeat_first_proposal = repeat_first_proposal
        self.labels: list[str] = []
        self.phases: list[str] = []
        self.prompts: list[str] = []
        self._next = 1
        self._proposals = 0

    async def phase(self, name: str) -> None:
        self.phases.append(name)

    def tokens_spent(self) -> int:
        return 0

    def tokens_remaining(self) -> float:
        return 10**9

    async def agent(self, prompt: str, *, label: str, tools: list[Any], schema: dict | None = None, **_: Any):
        self.labels.append(label)
        self.prompts.append(prompt)
        if schema is not None:
            self._proposals += 1
            n = int(prompt.split("exactly ")[1].split()[0])
            if self.repeat_first_proposal and self._proposals == 1:
                return {"inputs": [[1, -3]] * n}  # duplicates within the batch: must be rejected
            batch = []
            for _ in range(n):
                self._next += 1
                batch.append([self._next, -3])
            return {"inputs": batch}
        program = self.programs.pop(0) if len(self.programs) > 1 else self.programs[0]
        (self.workspace / "candidate.py").write_text(program, encoding="utf-8")
        return "done"


class SuppliedInputContext(ScriptedContext):
    """Return caller-owned inputs without copying them in the scripted agent."""

    def __init__(self, workspace: Path, inputs: list[Any], program: str) -> None:
        super().__init__(workspace, programs=[program])
        self.inputs = inputs

    async def agent(self, prompt: str, *, schema: dict | None = None, **kwargs: Any):
        if schema is not None:
            return {"inputs": self.inputs}
        return await super().agent(prompt, schema=schema, **kwargs)


def _run(task: ReconstructionTask, ctx: ScriptedContext, **args: Any) -> dict[str, Any]:
    return asyncio.run(reconstruct(ctx, {"task": task, **args}))


def test_registered_as_builtin() -> None:
    names = {spec.name for spec in get_builtin_workflows().list_specs()}
    assert "reconstruct" in names
    assert list(reconstruct.__workflow_spec__.phases) == ["probe", "code", "select"]


@pytest.mark.parametrize("random_share", [0.0, 0.5])
def test_correct_program_promotes_each_round_until_early_stop(tmp_path: Path, random_share: float) -> None:
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path)
    out = _run(task, ctx, random_share=random_share)

    batches = [INITIAL_BATCH + 2 * i for i in range(PROMOTION_EARLY_STOP)]
    assert out["stop_reason"] == "early_stop"
    assert out["iterations"] == out["promotions"] == PROMOTION_EARLY_STOP
    assert out["known_examples"] == sum(batches)
    assert out["random_inputs"] == (sum(b // 2 for b in batches) if random_share else 0)
    assert out["final_program"] == "iter_001.py" or out["final_program"].startswith("iter_")
    assert (tmp_path / "candidate.py").read_text() == CORRECT
    assert ctx.phases[0] == "probe" and ctx.phases[-1] == "select"
    assert all(src in ("agent", "random") for _, _, src in out["examples"])


def test_rejected_proposals_are_retried_and_recorded(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path, repeat_first_proposal=True)
    out = _run(task, ctx)
    rejected = [e for e in out["events"] if "rejected" in e]
    assert rejected and "repeat" in rejected[0]["rejected"]
    assert out["stop_reason"] == "early_stop"
    assert any("previous proposal was rejected" in p for p in ctx.prompts)


def test_failing_candidate_gets_feedback_and_no_batch(tmp_path: Path) -> None:
    wrong = "def solve(xs):\n    return 0\n"
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path, programs=[wrong, CORRECT])
    out = _run(task, ctx)
    iterations = [e for e in out["events"] if "iteration" in e]
    first, second = iterations[0], iterations[1]
    assert first["iteration"] == 1 and not first["promoted"] and first["known"] == INITIAL_BATCH
    assert second["iteration"] == 2 and second["promoted"] and second["known"] == INITIAL_BATCH
    assert any("fails these known examples" in p for p in ctx.prompts)


@pytest.mark.parametrize("random_share", [0.0, 1.0])
@pytest.mark.parametrize("nested", [False, True])
def test_mutating_oracle_preserves_input_and_rejects_identity(
    tmp_path: Path, random_share: float, nested: bool
) -> None:
    inputs = [{"values": [[1, 2]]}, {"values": [[3, 4]]}] if nested else [[1, 2], [3, 4]]
    original = verification.jsonable(inputs)
    outputs = [{"values": [[2, 1]]}, {"values": [[4, 3]]}] if nested else [[2, 1], [4, 3]]
    source = "random" if random_share else "agent"
    observed = []

    def oracle(inp: Any) -> Any:
        values = inp["values"][0] if nested else inp
        values.reverse()
        return inp

    task = make_task(
        tmp_path,
        oracle=oracle,
        random_inputs=lambda k, exclude: inputs,
        on_example=lambda inp, out, src: observed.append((inp, out, src)),
    )
    ctx = SuppliedInputContext(tmp_path, inputs, "def solve(xs):\n    return xs\n")
    out = _run(task, ctx, random_share=random_share, max_iterations=1, early_stop=1)

    assert inputs == original
    assert out["examples"] == [[inp, output, source] for inp, output in zip(original, outputs)]
    assert observed == [(inp, output, source) for inp, output in zip(original, outputs)]
    assert f"{original[0]!r} -> {outputs[0]!r}" in ctx.prompts[0]
    assert out["events"][-1]["passed"] == 0
    assert out["promotions"] == 0
    assert out["final_score"] == 0.0
    assert out["stop_reason"] == "max_iterations"


@pytest.mark.parametrize("random_share", [0.0, 1.0])
def test_oracle_output_aliases_preserve_each_observation(tmp_path: Path, random_share: float) -> None:
    inputs = [{"values": [1, 2]}, {"values": [3, 4]}]
    original = verification.jsonable(inputs)
    shared_output = {"values": []}
    oracle_inputs = []

    def oracle(inp: Any) -> Any:
        oracle_inputs.append(inp)
        shared_output["values"][:] = reversed(inp["values"])
        return shared_output

    task = make_task(tmp_path, oracle=oracle, random_inputs=lambda k, exclude: inputs)
    program = 'def solve(xs):\n    return {"values": list(reversed(xs["values"]))}\n'
    ctx = SuppliedInputContext(tmp_path, inputs, program)
    out = _run(task, ctx, random_share=random_share, max_iterations=1, early_stop=1)
    shared_output["values"][:] = [99]
    oracle_inputs[0]["values"].append(99)

    source = "random" if random_share else "agent"
    assert inputs == original
    assert out["examples"] == [
        [{"values": [1, 2]}, {"values": [2, 1]}, source],
        [{"values": [3, 4]}, {"values": [4, 3]}, source],
    ]
    assert out["promotions"] == 1
    assert out["stop_reason"] == "early_stop"


@pytest.mark.parametrize("random_share", [0.0, 1.0])
def test_oracle_keeps_input_types_from_each_source(tmp_path: Path, random_share: float) -> None:
    inputs = [(1, 2), (3, 4)]
    input_type = tuple if random_share else list
    received_types = []

    def oracle(inp: Any) -> int:
        received_types.append(type(inp))
        if not isinstance(inp, input_type):
            raise TypeError("Unexpected input type")
        return sum(inp)

    task = make_task(tmp_path, oracle=oracle, random_inputs=lambda k, exclude: inputs)
    ctx = SuppliedInputContext(tmp_path, inputs, "def solve(xs):\n    return sum(xs)\n")
    out = _run(task, ctx, random_share=random_share, max_iterations=1, early_stop=1)

    assert received_types == [input_type, input_type]
    assert all(isinstance(inp, input_type) for inp, _, _ in out["examples"])
    assert out["promotions"] == 1
    assert out["stop_reason"] == "early_stop"


@pytest.mark.parametrize("kind", ["points", "list", "two_lists", "int", "pair"])
@pytest.mark.parametrize("domain_check", [True, False])
def test_domain_check_retries_out_of_range_proposals(tmp_path: Path, kind: str, domain_check: bool) -> None:
    if kind == "points":
        invalid, valid = [[[-1, 0]], [[0, 2]]], [[[0, 0]], [[1, 1]]]
    elif kind == "two_lists":
        invalid, valid = [[[-1], [0]], [[0], [2]]], [[[0], [0]], [[1], [1]]]
    elif kind == "int":
        invalid, valid = [-1, 2], [0, 1]
    else:
        invalid, valid = [[-1, 0], [0, 2]], [[0, 0], [1, 1]]
    proposals = [invalid, valid]
    observed = []

    def total(inp: Any) -> int:
        return sum(total(v) for v in inp) if isinstance(inp, list) else inp

    def oracle(inp: Any) -> int:
        observed.append(inp)
        return total(inp)

    class ProposalContext(ScriptedContext):
        async def agent(self, prompt: str, *, schema: dict | None = None, **kwargs: Any):
            if schema is not None:
                self.labels.append(kwargs["label"])
                self.prompts.append(prompt)
                return {"inputs": proposals.pop(0)}
            return await super().agent(prompt, schema=schema, **kwargs)

    program = "def solve(inp):\n    return sum(solve(v) for v in inp) if isinstance(inp, list) else inp\n"
    task = make_task(tmp_path, oracle=oracle, kind=kind, lo=0, hi=1)
    ctx = ProposalContext(tmp_path, programs=[program])
    out = _run(task, ctx, domain_check=domain_check, max_iterations=1, early_stop=1)

    expected = valid if domain_check else invalid
    assert observed == expected
    assert out["examples"] == [[inp, total(inp), "agent"] for inp in expected]
    assert out["stop_reason"] == "early_stop" and out["promotions"] == 1
    rejected = [e for e in out["events"] if "rejected" in e]
    if domain_check:
        assert ctx.labels == ["propose:0.1", "propose:0.2", "code:1"]
        assert len(rejected) == 1 and "outside the valid range [0, 1]" in rejected[0]["rejected"]
        assert "previous proposal was rejected" in ctx.prompts[1]
    else:
        assert ctx.labels == ["propose:0.1", "code:1"]
        assert rejected == []


def test_coverage_gate_withholds_promotion_until_categories_are_covered(tmp_path: Path) -> None:
    task = make_task(tmp_path, random_inputs=None)
    ctx = ScriptedContext(tmp_path)
    out = _run(task, ctx, coverage_gate=True)
    # The scripted proposer never sends an empty list, so "len:0" stays open
    # for COVERAGE_GATE_PATIENCE batches before promotions count again.
    first = [e for e in out["events"] if e.get("iteration") == 1][0]
    assert "len:0" in first["uncovered"]
    assert out["iterations"] > PROMOTION_EARLY_STOP
    assert any("do not yet cover these case types" in p for p in ctx.prompts)


def test_memorisation_gate_demands_a_rewrite(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path, programs=[MEMORISING, MEMORISING, MEMORISING, CORRECT])
    out = _run(task, ctx, memorisation_gate=True, initial_batch=4)
    memorised = [e for e in out["events"] if e.get("memorised", 0) >= verification.MEMORISED_INPUTS_THRESHOLD]
    assert len(memorised) >= MEMORISATION_PATIENCE, out["events"]
    assert not memorised[0]["promoted"] or memorised[0]["known"] == 4
    assert sum(1 for p in ctx.prompts if "embedding" in p) >= MEMORISATION_PATIENCE
    assert out["stop_reason"] == "early_stop"


def test_budget_exhaustion_stops_the_loop_after_archiving_the_last_candidate(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path)
    ctx.tokens_remaining = lambda: 0  # type: ignore[method-assign]
    out = _run(task, ctx)
    assert out["stop_reason"] == "budget_exhausted" and out["iterations"] == 1
    # The program written in the final round is evaluated, archived and selected.
    assert [e["iteration"] for e in out["events"] if "iteration" in e] == [1]
    assert out["events"][-1]["promoted"] is True
    assert (tmp_path / ".oc_candidates" / "iter_001.py").is_file()
    assert out["final_program"] == "iter_001.py"
    assert (tmp_path / "candidate.py").read_text(encoding="utf-8") == CORRECT


def test_selection_ignores_candidates_left_by_an_earlier_run(tmp_path: Path) -> None:
    stale_dir = tmp_path / ".oc_candidates"
    stale_dir.mkdir()
    stale = "def solve(xs):\n    return 0\n"
    (stale_dir / "iter_050.py").write_text(stale, encoding="utf-8")
    task = make_task(tmp_path)
    ctx = ScriptedContext(tmp_path)
    ctx.tokens_remaining = lambda: 0  # type: ignore[method-assign]
    out = _run(task, ctx)
    assert out["final_program"] == "iter_001.py"
    assert (tmp_path / "candidate.py").read_text(encoding="utf-8") == CORRECT
    assert (stale_dir / "iter_050.py").read_text(encoding="utf-8") == stale  # left untouched


def test_task_resolution_from_import_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "fake_bench.py"
    module.write_text(
        "from pathlib import Path\n"
        "from opencollab.builtin_workflows import ReconstructionTask\n"
        "async def evaluate(program, inputs):\n    return [{'ok': True, 'output': 0} for _ in inputs]\n"
        "def make(workspace):\n"
        "    return ReconstructionTask(input_type='int', input_constraints='', function_name='f',\n"
        "        workspace=Path(workspace), oracle=lambda x: x, evaluate=evaluate)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    task = resolve_task({"task": "fake_bench:make", "task_args": {"workspace": str(tmp_path)}})
    assert isinstance(task, ReconstructionTask) and task.function_name == "f"
    token = register_task(task)
    assert token.startswith("reconstruct-task:") and resolve_task({"task": token}) is task
    with pytest.raises(ValueError):
        resolve_task({"task": token})  # consumed
    with pytest.raises(ValueError):
        resolve_task({})
    with pytest.raises(ValueError):
        asyncio.run(
            reconstruct(
                ScriptedContext(tmp_path), {"task": make_task(tmp_path, random_inputs=None), "random_share": 0.5}
            )
        )


@pytest.mark.parametrize(
    "actual,expected,matched",
    [
        (float(2**53), 2**53 + 1, False),
        (float(10**30 + 1), 10**30 + 1, False),
        (2**53 + 1, 2**53 + 1, True),
        (2**53, 2**53 + 1, False),
        (1.0 + 1e-7, 1, False),
        (1.0, 1, True),
        ([float(2**53)], [2**53 + 1], False),
        (0.1 + 0.2, 0.3, True),
        (0.3 + 2e-6, 0.3, False),
        (1, 1.0 + 1e-7, True),
        (1, 1.0 + 2e-6, False),
        ("0.3", 0.3, False),
        (complex(0.3 + 1e-7), 0.3, False),
        ((1, 2), [1, 2], True),
        ([(1, 2)], [[1, 2]], True),
        ({"value": (1, 2)}, {"value": [1, 2]}, True),
        ((1, 2), (1, 3), False),
        (True, 1, False),
        (1, True, False),
        (False, 0, False),
        (0, False, False),
        (True, 1.0, False),
        (1.0, True, False),
        (True, True, True),
        (False, False, True),
        ([True], [1], False),
        ({"ok": True}, {"ok": 1}, False),
        ({"ok": True}, {"ok": True}, True),
        ({"value": 0.1 + 0.2}, {"value": 0.3}, True),
    ],
)
def test_output_comparison_preserves_integer_precision(actual: Any, expected: Any, matched: bool) -> None:
    assert verification.outputs_match(actual, expected) is matched


@pytest.mark.parametrize("float_result", [False, True])
def test_large_integer_oracle_rejects_rounded_float_candidate(tmp_path: Path, float_result: bool) -> None:
    expression = "2**53 + 2 * xs[0] + 1"
    program = f"def solve(xs):\n    return {f'float({expression})' if float_result else expression}\n"
    task = make_task(tmp_path, oracle=lambda xs: 2**53 + 2 * xs[0] + 1)
    ctx = ScriptedContext(tmp_path, programs=[program])
    out = _run(task, ctx, max_iterations=1, early_stop=1)

    assert out["events"][-1]["passed"] == (0 if float_result else INITIAL_BATCH)
    assert out["promotions"] == (0 if float_result else 1)
    assert out["stop_reason"] == ("max_iterations" if float_result else "early_stop")


def test_coverage_and_verification_helpers() -> None:
    assert coverage.categories("", "string") == set()
    assert coverage.categories("ab", "string") == {"str:short"}
    assert "str:short" in coverage.missing([""], "string", None, None)
    assert coverage.categories([], "points") == set()
    assert coverage.categories([[0, 0]], "points") >= {"pts:few"}
    assert "pts:few" in coverage.missing([[]], "points", None, None)
    assert "pts:zero" not in coverage.categories([[False, False]], "points")
    assert coverage.categories((True, False), "pair") == set()
    assert coverage.categories(("a", "b"), "pair") == set()
    assert coverage.categories((1, 1), "pair") >= {"pair:equal", "pair:one"}
    required = coverage.required("list", -1000, 1000)
    inputs: list[Any] = [[], [5], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [-7, 0, 7], [3, 3, 3]]
    covered = set().union(*(coverage.categories(i, "list") for i in inputs))
    assert coverage.missing(inputs, "list", -1000, 1000) == required - covered
    assert coverage.describe({"len:0"}) == "an empty list"
    assert verification.memorised_inputs(LOOKUP, [[1, -3], [2, -3], [3, -3]]) == 3
    assert verification.memorised_inputs(CORRECT, [[1, -3], [2, -3]]) == 0
    assert verification.in_domain([1, 5000], "list", -1000, 1000) == [5000]
    assert not verification.outputs_match([1], [1, 2])
    assert 0.0 <= verification.stage_score(CORRECT, 1.0, [([1, -3], 4)]) <= 1.0
    assert verification.stage_score(LOOKUP, 1.0, [([1, -3], 4), ([2, -3], 5)]) < verification.stage_score(
        CORRECT, 1.0, [([1, -3], 4), ([2, -3], 5)]
    )
