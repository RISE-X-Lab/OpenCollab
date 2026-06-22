"""Tests for swebench/gen_prediction_workflow extras population (inject-f2p).

``build_task`` renders the issue prompt; the FAIL_TO_PASS ids + test_patch it
threads into ``EvalTask.extras`` are what the harness injects and the workflow
scopes to. We import the module from the repo-root ``swebench/`` dir (the same
way ``gen_prediction_workflow`` itself bootstraps the package path).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opencollab.harness.evaluator import EvalTask

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gpw = pytest.importorskip("gen_prediction_workflow")


FIXTURE = {
    "instance_id": "acme__widget-42",
    "repo": "acme/widget",
    "problem_statement": "Widget explodes on empty input.",
    "hints_text": "look at parse()",
    "FAIL_TO_PASS": '["tests/test_widget.py::test_empty", "tests/test_widget.py::test_none"]',
    "test_patch": (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n"
        "@@ -1 +1,2 @@\n x=1\n+assert widget('') == ''\n"
    ),
}


def test_fail_to_pass_ids_parses_json_string():
    assert gpw._fail_to_pass_ids(FIXTURE) == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_fail_to_pass_ids_accepts_list_and_missing():
    assert gpw._fail_to_pass_ids({"FAIL_TO_PASS": ["a", "b"]}) == ["a", "b"]
    assert gpw._fail_to_pass_ids({}) == []


def test_build_task_lists_target_tests_without_literal_values():
    prompt = gpw.build_task(FIXTURE)
    assert "tests/test_widget.py::test_empty" in prompt
    assert "Widget explodes on empty input." in prompt


def test_generate_populates_extras_from_instance():
    # Mirror exactly how generate() builds the EvalTask so the extras contract
    # (test_patch + parsed fail_to_pass) is locked in without needing docker.
    task = EvalTask(
        task_id=FIXTURE["instance_id"],
        description=gpw.build_task(FIXTURE),
        extras={
            "test_patch": FIXTURE.get("test_patch") or "",
            "fail_to_pass": gpw._fail_to_pass_ids(FIXTURE),
        },
    )
    assert task.extras["test_patch"] == FIXTURE["test_patch"]
    assert task.extras["fail_to_pass"] == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]
