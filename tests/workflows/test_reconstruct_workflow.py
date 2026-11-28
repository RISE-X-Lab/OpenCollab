"""The reconstruct workflow: batches, promotions, early stop, gates and task resolution (no model, no Docker)."""

from __future__ import annotations

import asyncio
import errno
import importlib
import json
import stat
import sys
from pathlib import Path
from typing import Any, Callable

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
    namespace: dict[str, Any] = {"__file__": str(program)}
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
    assert not list((tmp_path / ".oc_candidates").iterdir())


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

    def __init__(
        self,
        workspace: Path,
        programs: list[str] | None = None,
        repeat_first_proposal: bool = False,
        *,
        candidate_file: str = "candidate.py",
    ):
        self.workspace = workspace
        self.candidate_file = candidate_file
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
        (self.workspace / self.candidate_file).write_text(program, encoding="utf-8")
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


async def test_evaluated_candidate_survives_edit_to_live_program(tmp_path: Path) -> None:
    candidate_file = "src/candidate.py"
    program = tmp_path / candidate_file
    program.parent.mkdir()
    (program.parent / "offset.txt").write_text("5\n", encoding="utf-8")
    source = (
        "from pathlib import Path\n"
        "def solve(xs):\n"
        "    offset = int(Path(__file__).with_name('offset.txt').read_text())\n"
        "    return sum(abs(x) for x in xs) + offset\n"
    )
    evaluated, edited = asyncio.Event(), asyncio.Event()
    evaluated_paths = []

    class ExecutableCandidate(ScriptedContext):
        async def agent(self, *args, **kwargs):
            value = await super().agent(*args, **kwargs)
            if program.is_file():
                program.chmod(0o750)
            return value

    async def evaluate(path: Path, inputs: list[Any]) -> list[dict[str, Any]]:
        assert path != program
        assert path.parent == program.parent and path.suffix == program.suffix
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
        assert path.read_text(encoding="utf-8") == source
        results = await _evaluate(path, inputs)
        evaluated_paths.append(path)
        if len(evaluated_paths) == 1:
            evaluated.set()
            await edited.wait()
        return results

    async def edit_program() -> None:
        await evaluated.wait()
        program.write_text("def solve(xs):\n    return 0\n", encoding="utf-8")
        edited.set()

    task = make_task(
        tmp_path, candidate_file=candidate_file, evaluate=evaluate, oracle=lambda value: _oracle(value) + 5,
    )
    context = ExecutableCandidate(tmp_path, programs=[source], candidate_file=candidate_file)
    result, _ = await asyncio.wait_for(asyncio.gather(
        reconstruct(context, {"task": register_task(task), "early_stop": 1}),
        edit_program(),
    ), timeout=5)
    archived = tmp_path / ".oc_candidates" / result["final_program"]
    assert result["promotions"] == 1
    assert result["stop_reason"] == "early_stop"
    assert result["final_score"] == 1.0
    assert len(set(evaluated_paths)) == 2
    assert all(not path.exists() for path in evaluated_paths)
    assert set(program.parent.iterdir()) == {program, program.parent / "offset.txt"}
    assert archived.read_text(encoding="utf-8") == program.read_text(encoding="utf-8") == source
    assert stat.S_IMODE(archived.stat().st_mode) == stat.S_IMODE(program.stat().st_mode) == 0o750


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-workspace", "relative-workspace"])
@pytest.mark.parametrize("sidecar", [False, True], ids=["plain-module", "same-stem-resource"])
async def test_reconstruct_evaluator_can_run_the_candidate_as_a_module(tmp_path, monkeypatch, relative, sidecar):
    monkeypatch.chdir(tmp_path.parent)
    workspace = Path(tmp_path.name) if relative else tmp_path
    candidate_file = "src/candidate.py"
    package = workspace / "src"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "helper.py").write_text("def measure(xs):\n    return sum(abs(x) for x in xs)\n")
    if sidecar:
        (package / "candidate.json").write_text('{"offset": 5}', encoding="utf-8")
    source = "from .helper import measure\n"
    if sidecar:
        source += (
            "from pathlib import Path\n"
            "import json\n"
            "def solve(value):\n"
            "    offset = json.loads(Path(__file__).with_suffix('.json').read_text())['offset']\n"
            "    return measure(value) + offset\n"
        )
    else:
        source += "solve = measure\n"
    source += (
        "if __name__ == '__main__':\n"
        "    import json, sys\n"
        "    print(json.dumps([solve(value) for value in json.loads(sys.argv[1])]))\n"
    )
    evaluated = []

    async def evaluate(path, inputs):
        evaluated.append(path)
        module = ".".join(path.relative_to(workspace).with_suffix("").parts)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", module, json.dumps(inputs),
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return [{"ok": True, "output": value} for value in json.loads(stdout)]

    task = make_task(
        workspace, candidate_file=candidate_file, evaluate=evaluate,
        oracle=lambda value: _oracle(value) + (5 if sidecar else 0),
    )
    result = await reconstruct(
        ScriptedContext(workspace, programs=[source], candidate_file=candidate_file),
        {"task": register_task(task), "early_stop": 1},
    )
    assert result["promotions"] == 1 and result["stop_reason"] == "early_stop"
    assert len(evaluated) == 2
    assert all(not path.exists() for path in evaluated)
    assert all(not path.with_suffix(".json").is_symlink() for path in evaluated)
    assert (workspace / candidate_file).read_text(encoding="utf-8") == source
    assert (workspace / ".oc_candidates" / result["final_program"]).read_text(encoding="utf-8") == source


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-workspace", "relative-workspace"])
@pytest.mark.parametrize(
    ("neighbor", "evaluation_fails"),
    [("import", False), ("package", False), ("resource", False), ("resource", True)],
    ids=["sibling-import", "sibling-package", "sibling-resource", "evaluator-error"],
)
async def test_reconstruct_evaluates_a_writable_file_in_a_read_only_directory(
    tmp_path, monkeypatch, relative, neighbor, evaluation_fails,
):
    monkeypatch.chdir(tmp_path.parent)
    workspace = Path(tmp_path.name) if relative else tmp_path
    candidate_file = "src/solver/candidate.py"
    program = workspace / candidate_file
    program.parent.mkdir(parents=True)
    program.write_text("# Existing editable candidate.\n", encoding="utf-8")
    helper = program.with_name("sibling.py")
    helper.write_text("MULTIPLIER = 3\n", encoding="utf-8")
    package = program.with_name("sibling_package")
    package.mkdir()
    (package / "__init__.py").write_text("from .arithmetic import MULTIPLIER\n", encoding="utf-8")
    arithmetic = package / "arithmetic.py"
    arithmetic.write_text("MULTIPLIER = 3\n", encoding="utf-8")
    resource = program.with_name("multiplier.txt")
    resource.write_text("3", encoding="utf-8")
    dependency = {
        "import": "from sibling import MULTIPLIER\n",
        "package": "from sibling_package import MULTIPLIER\n",
        "resource": "from pathlib import Path\n"
        "MULTIPLIER = int(Path(__file__).with_name('multiplier.txt').read_text())\n",
    }[neighbor]
    source = dependency + (
        "import json, sys\n"
        "def solve(xs):\n    return MULTIPLIER * sum(abs(x) for x in xs)\n"
        "print(json.dumps([solve(value) for value in json.loads(sys.argv[1])]))\n"
    )
    module = importlib.import_module("opencollab.builtin_workflows.reconstruct")
    open_temporary = module.NamedTemporaryFile
    evaluated = []

    def deny_sibling_creation(*, dir, **kwargs):
        if dir == program.parent:
            raise PermissionError(errno.EACCES, "directory forbids new files")
        return open_temporary(dir=dir, **kwargs)

    async def evaluate(path, inputs):
        assert path.parent.parent == workspace / ".oc_candidates"
        assert path.name == program.name and not path.is_symlink()
        assert path.read_text(encoding="utf-8") == source
        for sibling in (helper, resource):
            link = path.with_name(sibling.name)
            assert not link.readlink().is_absolute()
            assert link.read_bytes() == sibling.read_bytes()
        package_link = path.with_name(package.name)
        assert not package_link.readlink().is_absolute()
        assert (package_link / arithmetic.name).read_bytes() == arithmetic.read_bytes()
        evaluated.append(path)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(path.relative_to(workspace)), json.dumps(inputs),
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        path.write_text("# Evaluator changes its own copy.\n", encoding="utf-8")
        if evaluation_fails:
            raise ValueError("evaluator returned invalid measurements")
        return [{"ok": True, "output": value} for value in json.loads(stdout)]

    monkeypatch.setattr(module, "NamedTemporaryFile", deny_sibling_creation)
    task = make_task(workspace, candidate_file=candidate_file, evaluate=evaluate, oracle=lambda xs: 3 * _oracle(xs))
    run = reconstruct(
        ScriptedContext(workspace, programs=[source], candidate_file=candidate_file),
        {"task": register_task(task), "early_stop": 1},
    )
    archives = workspace / ".oc_candidates"
    if evaluation_fails:
        with pytest.raises(ValueError, match="invalid measurements"):
            await run
        assert len(evaluated) == 1 and not list(archives.iterdir())
    else:
        result = await run
        archive = archives / result["final_program"]
        assert result["stop_reason"] == "early_stop"
        assert result["promotions"] == 1 and result["final_score"] > 0.95
        assert len(set(evaluated)) == 2
        assert set(archives.iterdir()) == {archive}
        assert archive.read_text(encoding="utf-8") == source
    assert all(not path.parent.exists() for path in evaluated)
    assert set(program.parent.iterdir()) == {program, resource, helper, package}
    assert set(package.iterdir()) == {package / "__init__.py", arithmetic}
    assert program.read_text(encoding="utf-8") == source


@pytest.mark.parametrize("cancel", [False, True], ids=["failure", "cancel"])
@pytest.mark.parametrize("kind", ["archive", "evaluation copy"])
async def test_failed_archive_cleanup_preserves_evaluator_exit(tmp_path, monkeypatch, cancel, kind):
    archive = tmp_path / ".oc_candidates" / "iter_001.py"
    started = asyncio.Event()
    evaluator_failure = RuntimeError("candidate evaluator lost its output stream")
    unlink = Path.unlink
    evaluated_paths = []
    sidecar = tmp_path / "candidate.json"
    sidecar.write_text('{"offset": 0}', encoding="utf-8")

    def fail_cleanup(path, *args, **kwargs):
        blocked = path == archive if kind == "archive" else path in evaluated_paths
        if blocked:
            raise OSError(errno.EIO, "candidate unlink failed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    async def evaluate(path, inputs):
        assert path.parent == tmp_path
        assert path.read_text(encoding="utf-8") == archive.read_text(encoding="utf-8")
        assert path.with_suffix(".json").read_bytes() == sidecar.read_bytes()
        evaluated_paths.append(path)
        assert all(row["ok"] for row in await _evaluate(path, inputs))
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise evaluator_failure

    task = make_task(tmp_path, evaluate=evaluate)

    async def run():
        try:
            await reconstruct(ScriptedContext(tmp_path), {"task": register_task(task)})
        except (RuntimeError, asyncio.CancelledError) as error:
            return error

    job = asyncio.create_task(run())
    try:
        if cancel:
            await asyncio.wait_for(started.wait(), timeout=5)
            job.cancel("cancel candidate evaluation")
        error = await job
        if cancel:
            assert isinstance(error, asyncio.CancelledError)
            assert str(error) == "cancel candidate evaluation"
        else:
            assert error is evaluator_failure
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    note, = error.__notes__
    evaluation_path, = evaluated_paths
    assert not evaluation_path.with_suffix(".json").is_symlink()
    assert sidecar.read_text(encoding="utf-8") == '{"offset": 0}'
    blocked = archive if kind == "archive" else evaluation_path
    assert f"candidate {kind} cleanup failed for {blocked.name}" in note
    assert "candidate unlink failed" in note
    assert blocked.read_text(encoding="utf-8") == CORRECT
    assert (evaluation_path if kind == "archive" else archive).exists() is False
    assert (tmp_path / "candidate.py").read_text(encoding="utf-8") == CORRECT


@pytest.mark.parametrize("overlap", [False, True], ids=["sequential", "parallel"])
async def test_shared_workspace_runs_keep_their_archived_candidates(tmp_path: Path, overlap: bool) -> None:
    first_selecting = asyncio.Event()
    second_finished = asyncio.Event()
    doubled = "def solve(xs):\n    return 2 * sum(abs(x) for x in xs)\n"

    class FirstContext(ScriptedContext):
        async def phase(self, name: str) -> None:
            await super().phase(name)
            if overlap and name == "select":
                first_selecting.set()
                await asyncio.wait_for(second_finished.wait(), timeout=5)

    async def run(ctx: ScriptedContext, oracle: Callable[[list[int]], int]) -> dict[str, Any]:
        task = make_task(tmp_path, candidate_file=ctx.candidate_file, oracle=oracle)
        return await reconstruct(ctx, {"task": register_task(task), "max_iterations": 1, "early_stop": 1})

    async def run_second() -> dict[str, Any]:
        if overlap:
            await asyncio.wait_for(first_selecting.wait(), timeout=5)
        ctx = ScriptedContext(tmp_path, [doubled], candidate_file="doubled.py")
        second = await run(ctx, lambda xs: 2 * _oracle(xs))
        second_finished.set()
        return second

    first_run = run(FirstContext(tmp_path, candidate_file="absolute.py"), _oracle)
    if overlap:
        first, second = await asyncio.gather(first_run, run_second())
    else:
        first, second = await first_run, await run_second()

    for outcome, filename, source in [(first, "absolute.py", CORRECT), (second, "doubled.py", doubled)]:
        assert outcome["promotions"] == 1
        assert outcome["final_score"] == 1.0
        assert (tmp_path / filename).read_text(encoding="utf-8") == source
        assert (tmp_path / ".oc_candidates" / outcome["final_program"]).read_text(encoding="utf-8") == source
    assert first["final_program"] == "iter_001.py"
    assert second["final_program"] != first["final_program"]


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
