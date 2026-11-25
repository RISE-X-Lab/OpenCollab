"""Check memorized inputs and complexity when ranking reconstruction candidates.

``stage_score`` is observed accuracy minus complexity and hard-coding penalties.
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


def _json_containers(value: Any) -> Any:
    """Treat tuples as JSON lists so a candidate can return either shape."""
    if isinstance(value, tuple):
        return [_json_containers(item) for item in value]
    if isinstance(value, list):
        return [_json_containers(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_containers(item) for key, item in value.items()}
    return value


def _is_real_number(value: Any) -> bool:
    """True for ints and floats. ``bool`` is an ``int`` subclass, not a number here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def outputs_match(actual: Any, expected: Any) -> bool:
    """Default output comparison: exact for ints and containers, 1e-6 tolerance for floats."""
    actual = _json_containers(actual)
    expected = _json_containers(expected)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(outputs_match(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            return False
        return all(outputs_match(actual[key], expected[key]) for key in expected)
    # Check bool before the numeric branch. Otherwise True matches 1 and False matches 0.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if _is_real_number(expected) and _is_real_number(actual):
        if isinstance(expected, int):
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
    if lo is None or hi is None or kind not in ("list", "two_lists", "int", "pair", "points"):
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
