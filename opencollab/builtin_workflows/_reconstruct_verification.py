"""Authority checks on a candidate program and the paper's stage score.

rea refuses to close an obligation on evidence without authority: a pass
produced by memorised fixtures proves nothing, and the more a claim is
inferred rather than observed, the more observation it needs. Two cheap,
deterministic checks implement that for Python candidates:

* ``memorised_inputs`` counts observed inputs embedded verbatim as literals
  (lookup-table keys, equality comparisons);
* the FromIO paper's complexity penalty: honest solutions in our runs stayed
  at or below 0.61 while every over-fitted program hit 1.0.

``stage_score`` is the paper's program score (accuracy on the observed
examples minus complexity and hard-coding penalties) used to pick the final
candidate.
"""

from __future__ import annotations

import ast
import json
from typing import Any

MEMORISED_INPUTS_THRESHOLD = 3  # distinct non-trivial observed inputs embedded as literals
COMPLEXITY_THRESHOLD = 0.8  # complexity penalty from which a pass needs more evidence
COMPLEXITY_PENALTY_WEIGHT = 0.1
SIMILARITY_PENALTY_WEIGHT = 0.1


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so tuples compare equal to lists."""
    return json.loads(json.dumps(value))


def outputs_match(actual: Any, expected: Any) -> bool:
    """Default output comparison: exact for ints and containers, 1e-6 tolerance for floats."""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(outputs_match(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, int) and isinstance(actual, int):
            return actual == expected
        return abs(actual - expected) < 1e-6
    return actual == expected


def _trivial(value: Any) -> bool:
    # Only whole container inputs of length >= 2 count. A scalar input can
    # coincide with a legitimate constant, and empty / single-element
    # containers are often spelled out honestly.
    if isinstance(value, (list, tuple, str)):
        return len(value) <= 1
    return True


def memorised_inputs(source: str, inputs: list[Any]) -> int:
    """How many distinct non-trivial observed inputs the source embeds verbatim."""
    known = {repr(jsonable(i)) for i in inputs if not _trivial(i)}
    if not known:
        return 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Constant)):
            try:
                value = ast.literal_eval(node)
            except (ValueError, TypeError, SyntaxError):
                continue
            try:
                key = repr(jsonable(value))
            except (TypeError, ValueError):
                continue
            if key in known:
                found.add(key)
    return len(found)


def in_domain(inp: Any, kind: str, lo: float | None, hi: float | None) -> list[Any]:
    """Values of ``inp`` outside the numeric range (empty = valid)."""
    if lo is None or hi is None or kind not in ("list", "two_lists", "int", "pair"):
        return []

    def nums(v: Any) -> list[Any]:
        if isinstance(v, bool):
            return []
        if isinstance(v, (int, float)):
            return [v]
        if isinstance(v, (list, tuple)):
            return [x for item in v for x in nums(item)]
        return []

    return [v for v in nums(inp) if v < lo or v > hi]


def complexity_penalty(source: str) -> float:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1.0
    branch = loop = func = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.Match)):
            branch += 1
        elif isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            loop += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func += 1
    size_term = max(0.0, min(1.0, (len(source) - 400) / 1600))
    structure_term = min(1.0, 0.03 * branch + 0.04 * loop + 0.02 * max(0, func - 1))
    return min(1.0, size_term + structure_term)


def _example_literals(value: Any) -> set[str]:
    literals: set[str] = set()
    rendered = repr(value)
    if len(rendered) <= 120:
        literals.add(rendered)
    if isinstance(value, (list, tuple, set)):
        for item in value:
            literals.update(_example_literals(item))
    elif isinstance(value, dict):
        for k, v in value.items():
            literals.update(_example_literals(k))
            literals.update(_example_literals(v))
    return literals


def hardcode_penalty(source: str, examples: list[tuple[Any, Any]]) -> float:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1.0
    code_literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, (bool, type(None))):
                continue
            if isinstance(value, (int, float)) and abs(value) <= 2:
                continue
            rendered = repr(value)
            if len(rendered) <= 120:
                code_literals.add(rendered)
    test_literals: set[str] = set()
    for inp, out in examples:
        test_literals.update(_example_literals(inp))
        test_literals.update(_example_literals(out))
    if not code_literals or not test_literals:
        return 0.0
    overlap = len(code_literals & test_literals)
    return min(1.0, overlap / max(4, len(code_literals)))


def stage_score(source: str, accuracy: float, examples: list[tuple[Any, Any]]) -> float:
    """The FromIO paper's program score with weights 1.0 / 0.1 / 0.1."""
    score = (
        accuracy
        - COMPLEXITY_PENALTY_WEIGHT * complexity_penalty(source)
        - SIMILARITY_PENALTY_WEIGHT * hardcode_penalty(source, examples)
    )
    return max(0.0, score)
