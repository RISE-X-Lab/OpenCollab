"""Behavior tests for PR and pushed-commit title validation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.check_conventional_title import validate_title

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_conventional_title.py"
_CLEAN_SNAPSHOT = "chore: \u5efa\u7acb\u5e72\u51c0\u53d1\u5e03\u5feb\u7167"


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path, subject: str) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Title Test")
    _git(repository, "config", "user.email", "title@example.invalid")
    (repository / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", subject)
    return repository


def _run(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def test_title_accepts_repository_convention():
    assert validate_title(_CLEAN_SNAPSHOT) is None
    assert validate_title("fix(runtime)!: \u4fee\u590d\u4f1a\u8bdd\u7ec8\u6001") is None


def test_title_rejects_invalid_type_english_summary_and_multiple_lines():
    assert validate_title("change: \u66f4\u65b0") == "title must follow Conventional Commits"
    assert validate_title("fix: repair runtime") == "title summary must contain Chinese text"
    assert validate_title("fix: \u4fee\u590d\nsecond line") == "title must be a single line"


def test_commit_mode_reads_the_subject_from_git(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--commit", "HEAD")

    assert result.returncode == 0
    assert "check passed" in result.stdout


def test_commit_mode_fails_when_the_commit_is_missing(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--commit", "missing")

    assert result.returncode == 2
    assert "Unable to read commit title" in result.stdout
