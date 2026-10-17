"""Agent-facing tools and candidate execution for IO2Code tasks.

Both tools run in the harness process. The agent's environment never holds
the oracle or the held-out tests; candidate programs are executed inside the
agent's own Docker container (``DockerCandidateRunner``), so model-written
code never runs on the host and cannot see the benchmark checkout.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opencollab.adapters.tools.base import Tool

from .tasks import Task, jsonable, outputs_match

MAX_QUERIES_PER_CALL = 20
CANDIDATE_TIMEOUT_SECONDS = 30  # same as the paper's evaluator timeout
MAX_REPORTED_FAILURES = 5
RUNNER_DIR = ".oc_runner"  # inside the workspace: visible to the container, ignored by git

_RUNNER = r"""
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("candidate", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fn = getattr(mod, sys.argv[2])


def coerce(value):
    # numpy scalars/arrays and sets are not JSON types; a candidate that
    # returns them is still compared by value, not rejected for the type.
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"unserialisable output of type {type(value).__name__}")


results = []
for inp in json.load(open(sys.argv[3])):
    try:
        out = json.loads(json.dumps(fn(inp), default=coerce))
        results.append({"ok": True, "output": out})
    except Exception as exc:  # report, do not abort the batch
        results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
json.dump(results, open(sys.argv[4], "w"))
"""


class CandidateRunner(Protocol):
    """Execute a candidate program (a path inside the workspace) on inputs."""

    async def run(self, program: Path, function_name: str, inputs: list[Any]) -> list[dict[str, Any]]: ...


class DockerCandidateRunner:
    """Run candidates with the container's Python inside the agent's environment.

    The workspace is bind-mounted at ``/workspace``; runner script, inputs and
    outputs go through ``<workspace>/.oc_runner`` so nothing is copied and the
    container never needs network or the benchmark checkout.
    """

    def __init__(
        self, environment: Any, workspace: Path, container_workspace: str = "/workspace", python: str = "python3"
    ) -> None:
        self._env = environment
        self._workspace = workspace.resolve()
        self._cws = container_workspace.rstrip("/")
        self._python = python

    def _container_path(self, host_path: Path) -> str:
        rel = host_path.resolve().relative_to(self._workspace)
        return f"{self._cws}/{rel.as_posix()}"

    async def run(self, program: Path, function_name: str, inputs: list[Any]) -> list[dict[str, Any]]:
        if not program.is_file():
            return [{"ok": False, "error": f"{program.name} missing"} for _ in inputs]
        rdir = self._workspace / RUNNER_DIR
        rdir.mkdir(exist_ok=True)
        (rdir / "runner.py").write_text(_RUNNER, encoding="utf-8")
        (rdir / "in.json").write_text(json.dumps(inputs), encoding="utf-8")
        out_file = rdir / "out.json"
        out_file.unlink(missing_ok=True)
        cmd = (
            f"cd {self._cws} && PYTHONDONTWRITEBYTECODE=1 {self._python} -I "
            f"{self._cws}/{RUNNER_DIR}/runner.py {self._container_path(program)} {function_name} "
            f"{self._cws}/{RUNNER_DIR}/in.json {self._cws}/{RUNNER_DIR}/out.json"
        )
        result = await self._env.exec_cmd(cmd, timeout=CANDIDATE_TIMEOUT_SECONDS)
        if getattr(result, "timed_out", False):
            return [{"ok": False, "error": "Timeout"} for _ in inputs]
        if result.returncode != 0 or not out_file.is_file():
            err = (result.stderr or result.stdout or "").strip().splitlines()
            msg = err[-1] if err else f"exit code {result.returncode}"
            return [{"ok": False, "error": msg} for _ in inputs]
        return json.loads(out_file.read_text(encoding="utf-8"))


class InProcessCandidateRunner:
    """Import and call the candidate in this process. Tests only: never use with model-written code."""

    async def run(self, program: Path, function_name: str, inputs: list[Any]) -> list[dict[str, Any]]:
        import importlib.util

        if not program.is_file():
            return [{"ok": False, "error": f"{program.name} missing"} for _ in inputs]
        spec = importlib.util.spec_from_file_location("candidate_under_test", program)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        try:
            spec.loader.exec_module(module)
            fn = getattr(module, function_name)
        except Exception as exc:  # noqa: BLE001 - reported per input like the container runner
            return [{"ok": False, "error": f"{type(exc).__name__}: {exc}"} for _ in inputs]
        results = []
        for inp in inputs:
            try:
                results.append({"ok": True, "output": jsonable(fn(inp))})
            except Exception as exc:  # noqa: BLE001
                results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return results


@dataclass
class QueryLog:
    """Every oracle answer known during one task, in order.

    In fixed mode the visible examples are preloaded; ``count`` only reports
    what was actually queried.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    preloaded: int = 0

    @property
    def count(self) -> int:
        return len(self.entries) - self.preloaded


def _parse_inputs(raw: Any) -> list[Any]:
    # Accept a JSON array, or a Python-literal string the model may produce.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = ast.literal_eval(raw)
    if not isinstance(raw, list):
        raise ValueError("inputs must be a JSON array of input values")
    return raw


class QueryOracleTool(Tool):
    """Feed inputs to the hidden program and return its outputs (free exploration mode only)."""

    name = "query_oracle"
    parameters = {
        "type": "object",
        "properties": {
            "inputs": {
                "type": "array",
                "description": (
                    f"Up to {MAX_QUERIES_PER_CALL} input values to feed to the hidden program, "
                    "each in the task's input format."
                ),
                "items": {},
            }
        },
        "required": ["inputs"],
    }

    def __init__(self, task: Task, log: QueryLog, max_queries: int) -> None:
        self._task = task
        self._log = log
        self._max_queries = max_queries
        self.description = (
            "Run the hidden program on a batch of inputs and get its outputs. "
            f"Input type: {task.input_type}. {task.input_constraints} "
            f"Total query budget for this task: {max_queries}."
        )

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        try:
            inputs = _parse_inputs(params.get("inputs"))
        except (ValueError, SyntaxError) as exc:
            return f"Error: {exc}"
        if len(inputs) > MAX_QUERIES_PER_CALL:
            return f"Error: at most {MAX_QUERIES_PER_CALL} inputs per call."
        remaining = self._max_queries - self._log.count
        if remaining <= 0:
            return "Error: oracle query budget exhausted."
        inputs = inputs[:remaining]
        lines = []
        for inp in inputs:
            try:
                out = self._task.oracle(inp)
                self._log.entries.append({"input": inp, "output": out})
                lines.append(f"{inp!r} -> {out!r}")
            except Exception as exc:  # noqa: BLE001 - the hidden program rejected this input
                self._log.entries.append({"input": inp, "error": type(exc).__name__})
                lines.append(f"{inp!r} -> ERROR {type(exc).__name__}")
        left = self._max_queries - self._log.count
        return "\n".join(lines) + f"\n({left} queries remaining)"


class CheckCandidateTool(Tool):
    """Compare the agent's candidate program against all oracle answers seen so far."""

    name = "check_candidate"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the candidate .py file inside the workspace.",
            }
        },
        "required": ["path"],
    }

    def __init__(self, task: Task, log: QueryLog, workspace: Path, runner: CandidateRunner) -> None:
        self._task = task
        self._log = log
        self._workspace = workspace
        self._runner = runner
        evidence = "every known input/output pair" if log.preloaded else "every input already sent to the oracle"
        self.description = (
            f"Run your candidate program on {evidence} and report which outputs "
            f"differ. It must define {task.function_name}(input_data). Use it before you finish."
        )

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        rel = str(params.get("path", "")).strip()
        program = (self._workspace / rel).resolve()
        if not program.is_relative_to(self._workspace) or not program.is_file():
            return f"Error: {rel!r} is not a file inside the workspace."
        cases = [e for e in self._log.entries if "output" in e]
        if not cases:
            return "No input/output pairs recorded yet."
        results = await self._runner.run(program, self._task.function_name, [c["input"] for c in cases])
        failures = []
        for case, res in zip(cases, results):
            if not res["ok"]:
                failures.append(f"{case['input']!r}: expected {case['output']!r}, got {res['error']}")
            elif not outputs_match(res["output"], jsonable(case["output"])):
                failures.append(f"{case['input']!r}: expected {case['output']!r}, got {res['output']!r}")
        passed = len(cases) - len(failures)
        head = f"{passed}/{len(cases)} recorded oracle cases match."
        if not failures:
            return head
        shown = "\n".join(failures[:MAX_REPORTED_FAILURES])
        more = (
            f"\n... and {len(failures) - MAX_REPORTED_FAILURES} more" if len(failures) > MAX_REPORTED_FAILURES else ""
        )
        return f"{head}\nFirst mismatches:\n{shown}{more}"
