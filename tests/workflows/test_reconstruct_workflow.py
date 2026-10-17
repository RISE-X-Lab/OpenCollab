"""The reconstruct workflow: batches, promotions, early stop, gates and task resolution (no model, no Docker)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from opencollab.builtin_workflows import ReconstructionTask, get_builtin_workflows, reconstruct, register_task
from opencollab.builtin_workflows import _reconstruct_coverage as coverage
from opencollab.builtin_workflows import _reconstruct_verification as verification
from opencollab.builtin_workflows.reconstruct import (
    INITIAL_BATCH,
    MEMORISATION_PATIENCE,
    PROMOTION_EARLY_STOP,
    resolve_task,
)

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


def test_coverage_and_verification_helpers() -> None:
    required = coverage.required("list", -1000, 1000)
    inputs: list[Any] = [[], [5], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [-7, 0, 7], [3, 3, 3]]
    covered = set().union(*(coverage.categories(i, "list") for i in inputs))
    assert coverage.missing(inputs, "list", -1000, 1000) == required - covered
    assert coverage.describe({"len:0"}) == "an empty list"
    assert verification.memorised_inputs(LOOKUP, [[1, -3], [2, -3], [3, -3]]) == 3
    assert verification.memorised_inputs(CORRECT, [[1, -3], [2, -3]]) == 0
    assert verification.in_domain([1, 5000], "list", -1000, 1000) == [5000]
    assert verification.outputs_match(0.1 + 0.2, 0.3) and not verification.outputs_match([1], [1, 2])
    assert 0.0 <= verification.stage_score(CORRECT, 1.0, [([1, -3], 4)]) <= 1.0
    assert verification.stage_score(LOOKUP, 1.0, [([1, -3], 4), ([2, -3], 5)]) < verification.stage_score(
        CORRECT, 1.0, [([1, -3], 4), ([2, -3], 5)]
    )
