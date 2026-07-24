"""Behavior tests for the added-file hygiene command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_added_files.py"


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Hygiene Test")
    _git(repository, "config", "user.email", "hygiene@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _run(
    repository: Path,
    base: str,
    head: str,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), base, head, *extra],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def test_hygiene_rejects_oversized_file_with_newline_name(tmp_path):
    repository, base = _repository(tmp_path)
    name = "oversized\nartifact.bin"
    (repository / name).write_bytes(b"x" * 512_001)
    _git(repository, "add", name)
    _git(repository, "commit", "-m", "add oversized file")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "oversized%0Aartifact.bin" in result.stdout
    assert "512001 bytes" in result.stdout


def test_hygiene_accepts_small_files_with_git_legal_special_names(tmp_path):
    repository, base = _repository(tmp_path)
    names = (
        "with space.txt",
        "with\ttab.txt",
        "with\nnewline.txt",
        'with"quote.txt',
        "café.txt",
    )
    for name in names:
        (repository / name).write_text("small\n", encoding="utf-8")
    _git(repository, "add", *names)
    _git(repository, "commit", "-m", "add special names")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 0
    assert "checks passed" in result.stdout


def test_hygiene_rejects_python_module_over_line_limit(tmp_path):
    repository, base = _repository(tmp_path)
    (repository / "large.py").write_text("pass\n" * 801, encoding="utf-8")
    _git(repository, "add", "large.py")
    _git(repository, "commit", "-m", "add large module")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "801 lines" in result.stdout


def test_hygiene_fails_when_git_diff_cannot_be_computed(tmp_path):
    repository, _base = _repository(tmp_path)

    result = _run(repository, "missing-base", "HEAD")

    assert result.returncode == 2
    assert "git diff failed" in result.stderr


def test_hygiene_checks_a_complete_root_tree(tmp_path):
    repository, _base = _repository(tmp_path)
    empty_tree = _git(repository, "hash-object", "-t", "tree", "/dev/null")

    result = _run(repository, empty_tree, "HEAD", "--require-files")

    assert result.returncode == 0
    assert "checks passed" in result.stdout


def test_hygiene_rejects_an_empty_complete_tree(tmp_path):
    repository, base = _repository(tmp_path)
    empty_tree = _git(repository, "hash-object", "-t", "tree", "/dev/null")

    result = _run(repository, base, empty_tree, "--require-files")

    assert result.returncode == 1
    assert "no files were available" in result.stdout
