"""Repository ownership guards for the framework-only source tree."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _REPO_ROOT / "tests"
_PACKAGE = _REPO_ROOT / "opencollab"
_COMPILED_DOCUMENT_SUFFIXES = (
    ".aux",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".out",
    ".pdf",
    ".synctex.gz",
    ".xdv",
)
_EVALUATION_OWNED_PATHS = (
    "configs/team.self.collab.yaml",
    "docs/eval_modes",
    "docs/experiments",
    "docs/monitoring",
    "docs/archive/2026-06-23-analyst-solve-deep-dive.md",
    "docs/archive/2026-06-23-analyst-solve-problem-catalog.md",
    "docs/swe_eval_harness_refactor.md",
    "evals",
    "eval_work",
    "swe_workdir",
    "swebench",
    "workflows",
)

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


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(imports)


def _visible_python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


def test_evaluation_implementation_is_owned_by_companion_package() -> None:
    for directory in (
        _REPO_ROOT / "opencollab" / "harness",
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


def test_evaluation_assets_are_owned_outside_the_framework_repository() -> None:
    offenders = [path for path in _EVALUATION_OWNED_PATHS if (_REPO_ROOT / path).exists()]
    assert offenders == []


def test_framework_docs_contain_no_compiled_artifacts() -> None:
    docs = _REPO_ROOT / "docs"
    offenders = sorted(
        str(path.relative_to(_REPO_ROOT))
        for path in docs.rglob("*")
        if path.is_file() and path.name.endswith(_COMPILED_DOCUMENT_SUFFIXES)
    )
    assert offenders == []


def test_evaluation_tests_are_owned_by_companion_package() -> None:
    offenders = sorted(
        path.name
        for path in _TESTS.glob("*.py")
        if path != Path(__file__) and path.name.startswith((*_EVAL_TEST_PREFIXES, *_EVAL_SUPPORT_PREFIXES))
    )
    assert offenders == []


def test_framework_and_tests_do_not_import_companion_implementations() -> None:
    forbidden = ("opencollab_eval", "swebench")
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {module}"
        for root in (_PACKAGE, _TESTS)
        for path in _visible_python_files(root)
        for module in _imports(path)
        if module in forbidden or module.startswith(tuple(f"{name}." for name in forbidden))
    ]
    assert offenders == []


def test_framework_scripts_contain_no_evaluation_entrypoints() -> None:
    scripts = _REPO_ROOT / "scripts"
    assert sorted(path.name for path in scripts.iterdir() if path.is_file() and not path.name.startswith(".")) == [
        "README.md",
        "check_added_files.py",
        "check_conventional_title.py",
        "check_dashscope.py",
        "check_interface_width.py",
        "check_secret_history.py",
        "demo_team_issue.sh",
        "generate_brand_assets.py",
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
