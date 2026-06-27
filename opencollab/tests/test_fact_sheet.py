"""STEP 5a/5c — deterministic pre-recon FACT SHEET + complexity-sized recon.

* T1 (FACT SHEET + INTEGRITY) — the NON-LLM extractor returns the correct
  signature / call-sites / imports / siblings / referenced types for a sample
  in-workspace file, AND its scanned file set provably EXCLUDES every answer
  artifact (``test_code/``, ``func_implementation*``, ``*_result.jsonl``,
  ``*_output.jsonl``) — even when those artifacts contain calls to the target.

* T2 (COMPLEXITY SIZING + off==reference) — sizing yields FEWER scouts for a
  trivial target than a complex one (ceiling respected); and with enforcement
  ``off`` ``_recon`` is byte-for-byte the reference path: unchanged scout count,
  unchanged per-scout cap, unchanged hints (no fact-sheet injection).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from opencollab.application.fact_sheet import (
    build_fact_sheet,
    estimate_target_complexity,
    format_fact_sheet_hint,
    is_answer_path,
    size_recon,
)
from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


def _recon_fn():
    """The live ``_recon`` from the analyst-solve module (shares its globals)."""
    fn = discover_workflows(str(_WF_DIR)).get("analyst-solve").fn
    return fn.__globals__["_recon"]


def _max_scouts() -> int:
    fn = discover_workflows(str(_WF_DIR)).get("analyst-solve").fn
    return fn.__globals__["MAX_SCOUTS"]


# --------------------------------------------------------------------------- #
# fixtures (built on disk; no network, no real LLM)
# --------------------------------------------------------------------------- #

_TARGET_SRC = '''\
import os
import math
from typing import Optional

CONST = 3


class Widget:
    """A widget."""

    def render(self) -> int:
        return 1


def helper(x):
    return x + 1


def compute_widget(a: int, b: Widget, *, mode: str = "x") -> int:
    """Compute the widget value for the given inputs and mode."""
    # TODO: implement this function
    raise NotImplementedError
'''

_CALLER_SRC = '''\
from pkg.target import compute_widget


def use():
    return compute_widget(1, None)
'''

# An answer artifact that CALLS the target — must never be scanned.
_TEST_CODE_SRC = '''\
from pkg.target import compute_widget


def test_it():
    assert compute_widget(1, None) == 2
'''

# The ground-truth implementation — must never be read.
_FUNC_IMPL_SRC = '''\
def compute_widget(a, b, mode="x"):
    return 999  # THE ANSWER — leaking this would defeat the benchmark
'''


def _build_rich_repo(root: Path) -> tuple[str, str]:
    """Create a realistic stubbed workspace with answer artifacts alongside.

    Returns ``(workspace_root, goal)``.
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "target.py").write_text(_TARGET_SRC, encoding="utf-8")
    (pkg / "caller.py").write_text(_CALLER_SRC, encoding="utf-8")
    # Answer artifacts (must be excluded):
    tc = root / "test_code"
    tc.mkdir()
    (tc / "test_target.py").write_text(_TEST_CODE_SRC, encoding="utf-8")
    (root / "func_implementation.py").write_text(_FUNC_IMPL_SRC, encoding="utf-8")
    (root / "task_result.jsonl").write_text('{"func_implementation": "..."}\n', encoding="utf-8")
    (root / "task_output.jsonl").write_text('{"answer": "..."}\n', encoding="utf-8")

    stub_abs = str(pkg / "target.py")
    goal = (
        "You are working in a repository for the demo framework.\n\n"
        "TASK: Implement the function `compute_widget`.\n\n"
        "IMPORTANT CONTEXT:\n"
        f"- The function stub is at: {stub_abs} (near line 19)\n"
        "INSTRUCTIONS: implement it.\n"
    )
    return str(root), goal


def _build_trivial_repo(root: Path) -> tuple[str, str]:
    (root / "mod.py").write_text(
        "def tiny(x):\n    # TODO: implement this function\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    stub_abs = str(root / "mod.py")
    goal = (
        "TASK: Implement the function `tiny`.\n"
        f"- The function stub is at: {stub_abs} (near line 1)\n"
    )
    return str(root), goal


# --------------------------------------------------------------------------- #
# T1 — fact sheet correctness + integrity exclusion
# --------------------------------------------------------------------------- #


def test_t1_fact_sheet_extracts_signature_calls_imports(tmp_path):
    root, goal = _build_rich_repo(tmp_path)
    m = build_fact_sheet(root, goal)
    assert m is not None

    # Signature + arity (a, b, mode — self/cls not counted; module-level here).
    assert m["function_name"] == "compute_widget"
    assert "def compute_widget(" in m["signature"]
    assert "mode" in m["signature"] and "-> int" in m["signature"]
    assert m["param_count"] == 3
    assert "Compute the widget value" in m["docstring"]
    assert m["target_file"] == os.path.join("pkg", "target.py")

    # Imports lifted from the target module.
    assert any("import os" in i for i in m["imports"])
    assert any("from typing import Optional" in i for i in m["imports"])

    # Sibling functions in the file + referenced type/class defs.
    assert "helper" in m["siblings"]
    assert "Widget" in m["referenced_types"]

    # Call sites: the real in-workspace caller is found...
    assert any(s.startswith(os.path.join("pkg", "caller.py")) for s in m["call_sites"])
    # ...and NO answer artifact leaks into the call sites (even though the
    # test_code/ test and func_implementation BOTH textually call compute_widget).
    assert not any("test_code" in s for s in m["call_sites"])
    assert not any("func_implementation" in s for s in m["call_sites"])


def test_t1_integrity_scanned_set_excludes_answer_paths(tmp_path):
    root, goal = _build_rich_repo(tmp_path)
    m = build_fact_sheet(root, goal)
    assert m is not None

    scanned = m["scanned_files"]
    assert scanned, "expected the extractor to record the files it read"
    # PROOF: not one scanned file is an answer artifact.
    assert all(not is_answer_path(f) for f in scanned)
    assert not any("test_code" in f for f in scanned)
    assert not any("func_implementation" in f for f in scanned)
    assert not any(f.endswith("_result.jsonl") or f.endswith("_output.jsonl") for f in scanned)
    # The legitimate source WAS scanned.
    assert os.path.join("pkg", "caller.py") in scanned


def test_t1_is_answer_path_predicate():
    assert is_answer_path("a/test_code/test_x.py")
    assert is_answer_path("test_code/conftest.py")
    assert is_answer_path("func_implementation.py")
    assert is_answer_path("pkg/func_implementation_v2.py")
    assert is_answer_path("runs/task_result.jsonl")
    assert is_answer_path("runs/task_output.jsonl")
    assert is_answer_path("/abs/path/test_code/x.py")
    # Legitimate source is NOT an answer path.
    assert not is_answer_path("pkg/target.py")
    assert not is_answer_path("src/widgets/compute.py")
    assert not is_answer_path("data/results.json")  # not *_result.jsonl


def test_t1_no_target_returns_none(tmp_path):
    # A goal that names no function -> graceful None (CLI / non-KOCO tasks).
    (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")
    assert build_fact_sheet(str(tmp_path), "Fix the bug in the parser.") is None
    # No workspace root -> None.
    assert build_fact_sheet(None, "TASK: Implement the function `f`.") is None


# --------------------------------------------------------------------------- #
# T2 — complexity sizing (unit) + off==reference parity in _recon
# --------------------------------------------------------------------------- #


def test_t2_sizing_trivial_gets_fewer_scouts_than_complex():
    ceiling = _max_scouts()
    trivial = {
        "param_count": 1,
        "docstring_len": 20,
        "call_site_count": 0,
        "call_sites": [],
        "referenced_types": [],
    }
    complex_ = {
        "param_count": 6,
        "docstring_len": 900,
        "call_site_count": 15,
        "call_sites": [f"f.py:{i}" for i in range(15)],
        "referenced_types": ["A", "B", "C", "D"],
    }
    n_t, leash_t = size_recon(4, estimate_target_complexity(trivial), ceiling=ceiling)
    n_c, leash_c = size_recon(4, estimate_target_complexity(complex_), ceiling=ceiling)

    assert n_t < n_c
    assert n_t == 1
    assert n_c == ceiling  # complex saturates the ceiling
    assert leash_t < leash_c  # trivial target is also depth-leashed harder
    # Ceiling is a hard cap even when many dims are requested.
    assert size_recon(99, estimate_target_complexity(complex_), ceiling=ceiling)[0] == ceiling


def test_t2_sizing_never_exceeds_dims():
    # A complex target but only 1 dimension -> 1 scout (never invents work).
    complex_ = {"param_count": 9, "docstring_len": 2000, "call_site_count": 40,
                "call_sites": [], "referenced_types": ["A", "B", "C"]}
    assert size_recon(1, estimate_target_complexity(complex_), ceiling=4)[0] == 1


# -- _recon parity / behavior harness --------------------------------------- #


class _Budget:
    total = 1_000_000

    def __init__(self, remaining: int) -> None:
        self._remaining = remaining

    def remaining(self) -> float:
        return float(self._remaining)

    def spent(self) -> int:
        return 0


class _ReconCtx:
    def __init__(self, *, workspace_root=None, remaining=1_000_000) -> None:
        self.workspace_root = workspace_root
        self.agent_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.budget = _Budget(remaining)

    async def agent(self, prompt, **kw):
        self.agent_calls.append({"prompt": prompt, **kw})
        return f"findings for {kw.get('label')}"

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def log(self, message):
        self.logs.append(message)


_THREE_DIMS = [
    {"aspect": "origin", "question": "where?", "hints": ["look in pkg/target.py"]},
    {"aspect": "contract", "question": "callers?", "hints": ["grep callers"]},
    {"aspect": "edges", "question": "edge cases?", "hints": ["the docstring"]},
]


def _scout_calls(ctx: _ReconCtx) -> list[dict[str, Any]]:
    return [c for c in ctx.agent_calls if str(c.get("label", "")).startswith("scout:")]


def test_t2_recon_off_is_reference(tmp_path):
    """enforcement off: scout count, cap, and hints unchanged (no fact sheet)."""
    recon = _recon_fn()
    # workspace_root is set, but OFF must never read it.
    root, _goal = _build_rich_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, remaining=1_000_000)
    asyncio.run(recon(ctx, "TASK: Implement the function `compute_widget`.", _THREE_DIMS, "off"))

    scouts = _scout_calls(ctx)
    assert len(scouts) == 3  # one per dimension (no complexity trimming)
    # Reference per-scout cap: min(250k, (1M-600k)//3).
    expected_cap = min(250_000, (1_000_000 - 600_000) // 3)
    assert all(c["budget"] == expected_cap for c in scouts)
    # Hints unchanged: the raw dim hint is present, the fact sheet is NOT injected.
    assert all("Pre-recon fact sheet" not in c["prompt"] for c in scouts)
    assert any("look in pkg/target.py" in c["prompt"] for c in scouts)
    assert any("grep callers" in c["prompt"] for c in scouts)


def test_t2_recon_on_trivial_trims_scouts_and_injects_fact_sheet(tmp_path):
    """enforcement on + a trivial target: 3 dims collapse to 1 leashed scout that
    carries the static fact sheet."""
    recon = _recon_fn()
    root, goal = _build_trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, remaining=1_000_000)
    asyncio.run(recon(ctx, goal, _THREE_DIMS, "needs-enforcement"))

    scouts = _scout_calls(ctx)
    assert len(scouts) == 1  # complexity sizing trimmed 3 -> 1
    # Depth-leashed cap: base min(250k, (1M-600k)//1) = 250k, leash 0.45.
    base = min(250_000, (1_000_000 - 600_000) // 1)
    assert scouts[0]["budget"] == max(1, int(base * 0.45))
    # The fact sheet was injected into the surviving scout.
    assert "Pre-recon fact sheet" in scouts[0]["prompt"]
    assert scouts[0]["enforcement_strength"] == "needs-enforcement"


def test_format_fact_sheet_hint_is_compact_and_safe(tmp_path):
    root, goal = _build_rich_repo(tmp_path)
    m = build_fact_sheet(root, goal)
    hint = format_fact_sheet_hint(m)
    assert "Pre-recon fact sheet" in hint
    assert "compute_widget" in hint
    # The rendered hint never names an answer artifact.
    assert "test_code" not in hint
    assert "func_implementation" not in hint
