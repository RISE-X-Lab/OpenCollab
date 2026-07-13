"""Repository ownership guard for the separately packaged evaluation layer."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS = _REPO_ROOT / "opencollab" / "tests"

_EVAL_TEST_PREFIXES = (
    "test_analyst_solve",
    "test_container_process_guard",
    "test_container_quiescence",
    "test_eval",
    "test_evaluator",
    "test_gen_prediction",
    "test_glm_token_monitor",
    "test_openhands",
    "test_phase1_toolset_whitelist",
    "test_phase2_warts",
    "test_solver_backend",
    "test_split_solve",
    "test_start_team_run",
    "test_swe",
    "test_test_injection",
    "test_token_cost_summary",
    "test_validation_council",
    "test_workflow_run_tests_controls",
)
_EVAL_SUPPORT_PREFIXES = (
    "evaluator_",
    "generation_proof_",
    "swe_eval_",
    "swe_v1_",
)
_ACTIVE_EVAL_COMMAND = re.compile(
    r"(?im)^\s*(?:[$>]\s*)?(?:(?:\S*/)?opencollab|(?:\S*/)?start_opencollab\.sh)\s+[^\n]*\beval\b"
)
_EVAL_FORWARDING_INSTRUCTION = re.compile(r"(?i)\bpass\s+`?eval\b")


def test_evaluation_implementation_is_owned_by_companion_package() -> None:
    for directory in (
        _REPO_ROOT / "opencollab" / "opencollab" / "harness",
        _REPO_ROOT / "swebench",
        _REPO_ROOT / "workflows",
    ):
        if directory.exists():
            visible_files = [
                path.relative_to(directory)
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and not any(part.startswith(".") for part in path.relative_to(directory).parts)
            ]
            assert visible_files == []


def test_evaluation_tests_are_owned_by_companion_package() -> None:
    offenders = sorted(
        path.name
        for path in _TESTS.glob("*.py")
        if path != Path(__file__)
        and path.name.startswith((*_EVAL_TEST_PREFIXES, *_EVAL_SUPPORT_PREFIXES))
    )
    assert offenders == []


def test_framework_scripts_contain_no_evaluation_entrypoints() -> None:
    scripts = _REPO_ROOT / "scripts"
    assert sorted(
        path.name for path in scripts.iterdir() if path.is_file() and not path.name.startswith(".")
    ) == [
        "README.md",
        "check_dashscope.py",
        "start_opencollab.sh",
    ]


def test_framework_scripts_do_not_advertise_removed_eval_commands() -> None:
    scripts = _REPO_ROOT / "scripts"
    documents = [scripts / "README.md", *sorted(scripts.glob("*.sh"))]
    offenders = [
        path.relative_to(_REPO_ROOT)
        for path in documents
        if _ACTIVE_EVAL_COMMAND.search(path.read_text(encoding="utf-8"))
        or _EVAL_FORWARDING_INSTRUCTION.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
