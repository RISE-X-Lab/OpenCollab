"""Host-side access to IO2Code tasks.

Everything here runs in the harness process, never inside the agent's
workspace: the oracle function and the held-out tests stay invisible to the
agent, which only reaches the oracle through the tools in ``tools.py``.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

IO2CODE_COMMIT = "ef15827"  # github.com/JiaruQian/IO2Code revision the example was validated against
_io2code_root: Path | None = None


def set_io2code_root(path: str | os.PathLike[str] | None) -> Path:
    """Point the example at a local IO2Code checkout (``--io2code`` or ``$IO2CODE_ROOT``)."""
    global _io2code_root
    raw = path or os.environ.get("IO2CODE_ROOT")
    if not raw:
        raise SystemExit(
            "IO2Code checkout not configured. Clone it and pass --io2code PATH or set IO2CODE_ROOT:\n"
            f"  git clone https://github.com/JiaruQian/IO2Code.git && git -C IO2Code checkout {IO2CODE_COMMIT}"
        )
    root = Path(raw).expanduser().resolve()
    if not (root / "DIO-Agent-experiments" / "tasks").is_dir():
        raise SystemExit(f"{root} does not look like an IO2Code checkout (missing DIO-Agent-experiments/tasks)")
    _io2code_root = root
    return root


def io2code_root() -> Path:
    return _io2code_root if _io2code_root is not None else set_io2code_root(None)


def experiments_dir() -> Path:
    return io2code_root() / "DIO-Agent-experiments"


def tasks_dir() -> Path:
    return experiments_dir() / "tasks"


@dataclass(frozen=True)
class Task:
    name: str
    function_name: str
    input_type: str
    input_constraints: str
    oracle: Callable[[Any], Any]
    held_out: list[tuple[Any, Any]]
    visible: list[tuple[Any, Any]]  # the 8 examples the paper's fixed mode shows
    kind: str  # structural input kind for coverage.py: list / two_lists / int / pair / string / points
    lo: float | None
    hi: float | None
    is_float: bool
    adapter: Any = field(repr=False, compare=False)

    def random_inputs(self, k: int, exclude: set[str] = frozenset()) -> list[Any]:
        """Inputs drawn from IO2Code's own generator (the benchmark's distribution).

        Independent of any hypothesis the agent holds, which is what makes
        them useful as verification evidence (rea's authority rule).
        """
        out: list[Any] = []
        seen = set(exclude)
        for _ in range(6):  # a few rounds of oversampling to get k distinct new inputs
            for inp, _out in self.adapter.get_input_output_examples(max(2 * k, 4)):
                inp = jsonable(inp)
                if repr(inp) in seen:
                    continue
                seen.add(repr(inp))
                out.append(inp)
                if len(out) == k:
                    return out
        return out


_EXAMPLE_RE = re.compile(
    r"Example\s+\d+\s*:\s*\n\s*Input:\s*(?P<input>[^\n]*)\n\s*Output:\s*(?P<output>[^\n]*)",
    re.MULTILINE,
)


def _visible_examples(task_dir: Path) -> list[tuple[Any, Any]]:
    # The visible examples only exist inside the DIO-Agent system prompt in
    # config.yaml; parse them the same way run_dio_agent.py does.
    import yaml

    config = yaml.safe_load((task_dir / "config.yaml").read_text(encoding="utf-8"))
    message = config.get("prompt", {}).get("system_message", "")

    def parse(raw: str) -> Any:
        raw = raw.strip()
        if raw == "":
            return ""
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw

    return [(parse(m.group("input")), parse(m.group("output"))) for m in _EXAMPLE_RE.finditer(message)]


def _task_adapter(name: str) -> Any:
    # IO2Code's adapter module (and the task packages it imports) live in the
    # benchmark checkout; import them from there, nothing is vendored.
    if str(experiments_dir()) not in sys.path:
        sys.path.insert(0, str(experiments_dir()))
    from adapter import TaskAdapter

    return TaskAdapter(name)


def _function_name(task_dir: Path) -> str:
    # Same rule as run_dio_agent._load_function_name: first top-level def.
    tree = ast.parse((task_dir / "initial_program.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            return node.name
    raise ValueError(f"no function definition in {task_dir / 'initial_program.py'}")


def _held_out_cases(task_dir: Path) -> list[tuple[Any, Any]]:
    # The 15 held-out cases live as a literal ``test_cases = [...]`` inside
    # evaluate(); read them without importing the DIO-Agent evaluator.
    tree = ast.parse((task_dir / "evaluator.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "test_cases"
        ):
            return [tuple(case) for case in ast.literal_eval(node.value)]
    raise ValueError(f"no test_cases literal in {task_dir / 'evaluator.py'}")


def _input_constraints(adapter: Any) -> str:
    # Mirrors run_active_dio_agent._oracle_input_constraints so the agent gets
    # the same input-format hint as the paper's active-oracle setting.
    if adapter.is_extra:
        task_type = adapter.task_info.get("type", "unknown")
        value_range = adapter.task_info.get("range")
        range_text = f", value range: {value_range}" if value_range is not None else ""
        return f"Task-specific input type: {task_type}{range_text}."
    dtype = getattr(adapter, "dtype", None)
    if dtype == "2d_int":
        return "Valid input is a list containing exactly two integer lists [xs, ys] of the same length."
    if dtype == "2d_bit":
        return "Valid input is a list containing exactly two bit lists [xs, ys] of the same length."
    if dtype == "int":
        return "Valid input is a list of integers."
    if dtype == "bit":
        return "Valid input is a list of bits."
    if dtype == "float":
        return "Valid input is a list of floats."
    return "Use valid inputs matching the stated input type."


def list_tasks() -> list[str]:
    """Task names in the checkout (all 125; the 10 multimodal ones need a vision model at runtime)."""
    return sorted(p.name for p in tasks_dir().iterdir() if (p / "evaluator.py").is_file())


# Input kinds of the built-in workflow's coverage gate, derived from IO2Code's dtype / type names.
_BASE_KINDS = {"int": "list", "bit": "list", "float": "list", "2d_int": "two_lists", "2d_bit": "two_lists"}
_EXTRA_KINDS = {
    "single_int": "int",
    "pair": "pair",
    "string": "string",
    "int_list": "list",
    "bit_seq": "list",
    "bit_list": "list",
    "rpn_seq": "list",
    "rpn_seq2": "list",
    "rpn_seq3": "list",
    "bit_pairs": "points",
    "point_pair": "points",
    "points_triplet": "points",
    "points_list": "points",
    "points_path": "points",
    "coin_change": "other",
}


def kind_of(dtype: str | None, extra_type: str | None) -> str:
    if extra_type is not None:
        return _EXTRA_KINDS.get(extra_type, "other")
    return _BASE_KINDS.get(dtype or "", "other")


def load_task(name: str) -> Task:
    task_dir = tasks_dir() / name
    if not task_dir.is_dir():
        raise SystemExit(f"unknown IO2Code task {name!r} (no directory {task_dir})")
    adapter = _task_adapter(name)
    oracle = adapter.task_info["func"] if adapter.is_extra else adapter.func
    if adapter.is_extra:
        dtype, extra_type = None, adapter.task_info.get("type")
        bounds = adapter.task_info.get("range") or (None, None)
    else:
        dtype, extra_type = adapter.dtype, None
        bounds = (0, 1) if dtype in ("bit", "2d_bit") else (adapter.bounds or (None, None))
        if dtype == "2d_int" and bounds[1] is not None:
            bounds = (bounds[0], bounds[1] - 1)  # the generator draws randint(lo, hi-1) for 2d_int
    return Task(
        name=name,
        function_name=_function_name(task_dir),
        input_type=adapter.get_input_type(),
        input_constraints=_input_constraints(adapter),
        oracle=oracle,
        held_out=_held_out_cases(task_dir),
        visible=_visible_examples(task_dir),
        kind=kind_of(dtype, extra_type),
        lo=bounds[0],
        hi=bounds[1],
        is_float=(dtype == "float"),
        adapter=adapter,
    )


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so tuples compare equal to lists.

    Candidate programs run in a subprocess and report outputs as JSON, so an
    oracle output such as ``(1, 2)`` must be normalised the same way before
    comparison.
    """
    import json

    return json.loads(json.dumps(value))


def outputs_match(actual: Any, expected: Any) -> bool:
    """Same comparison as IO2Code's evaluator (``_compare_outputs``)."""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(outputs_match(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, int) and isinstance(actual, int):
            return actual == expected
        return abs(actual - expected) < 1e-6
    return actual == expected
