"""Behavior tests for the proposed-history secret scan."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_secret_history.py"


def _script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_secret_history", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Security Test")
    _git(repository, "config", "user.email", "security@example.invalid")
    (repository / ".secrets.baseline").write_text("{}\n", encoding="utf-8")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")

    scanner = tmp_path / "fake-scanner.py"
    scanner.write_text(
        """#!/usr/bin/env python3
import pathlib
import subprocess
import sys

subprocess.run(
    ["git", "rev-parse", "--is-inside-work-tree"],
    check=True,
    capture_output=True,
)
if sys.argv[sys.argv.index("--baseline") + 1] != ".secrets.baseline":
    raise SystemExit(2)
paths = [name for name in sys.argv[sys.argv.index("--baseline") + 2:] if name != "--"]
for name in paths:
    if b"DEMO_SECRET" in pathlib.Path(name).read_bytes():
        raise SystemExit(1)
""",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    return repository, _git(repository, "rev-parse", "HEAD"), scanner


def _run(
    repository: Path,
    base: str,
    head: str,
    scanner: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            base,
            head,
            "--scanner-command",
            str(scanner),
            *extra,
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_trusted(
    repository: Path,
    commit: str,
    scanner: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--trusted-tree",
            commit,
            "--scanner-command",
            str(scanner),
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def test_secret_history_scans_secret_removed_by_later_commit(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / "temporary.txt").write_text("DEMO_SECRET\n", encoding="utf-8")
    _git(repository, "add", "temporary.txt")
    _git(repository, "commit", "-m", "add secret")
    (repository / "temporary.txt").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "remove secret")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 1
    assert "Secret scan failed for proposed commit" in result.stdout


def test_secret_history_rejects_baseline_change(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / ".secrets.baseline").write_text('{"changed": true}\n', encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "change baseline")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 1
    assert "baseline-only PR" in result.stdout


def test_secret_history_allows_labeled_baseline_only_change(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / ".secrets.baseline").write_text('{"changed": true}\n', encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "change baseline")

    result = _run(
        repository,
        base,
        "HEAD",
        scanner,
        "--allow-baseline-change",
    )

    assert result.returncode == 0


def test_secret_history_labeled_baseline_change_must_be_isolated(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / ".secrets.baseline").write_text('{"changed": true}\n', encoding="utf-8")
    (repository / "code.py").write_text("safe = True\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "mix baseline and code")

    result = _run(
        repository,
        base,
        "HEAD",
        scanner,
        "--allow-baseline-change",
    )

    assert result.returncode == 1
    assert "baseline-only PR" in result.stdout


def test_secret_history_accepts_clean_commits_and_special_names(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    name = "clean\nfile.txt"
    (repository / name).write_text("safe\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", "add clean file")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 0
    assert "checks passed" in result.stdout


def test_secret_history_accepts_line_metadata_only_baseline_refresh(tmp_path):
    repository, _base, _scanner = _repository(tmp_path)
    baseline = {
        "version": "1.5.0",
        "results": {
            "code.py": [
                {
                    "type": "Secret Keyword",
                    "filename": "code.py",
                    "hashed_secret": "stable-hash",
                    "line_number": 3,
                    "is_secret": False,
                }
            ]
        },
        "generated_at": "before",
    }
    (repository / ".secrets.baseline").write_text(
        json.dumps(baseline) + "\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "seed audited baseline")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "code.py").write_text("safe = True\n", encoding="utf-8")
    _git(repository, "add", "code.py")
    _git(repository, "commit", "-m", "shift source lines")

    scanner = tmp_path / "metadata-refresh-scanner.py"
    scanner.write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path

path = Path(".secrets.baseline")
data = json.loads(path.read_text())
for findings in data["results"].values():
    for finding in findings:
        finding["line_number"] += 8
data["generated_at"] = "after"
path.write_text(json.dumps(data) + "\\n")
raise SystemExit(3)
""",
        encoding="utf-8",
    )
    scanner.chmod(0o755)

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 0
    assert "checks passed" in result.stdout


def test_secret_history_rejects_zero_file_scan(tmp_path, monkeypatch, capsys):
    repository, base, scanner = _repository(tmp_path)
    module = _script_module()
    monkeypatch.setattr(module, "_materialize_tree", lambda *_args: [])

    result = module.check_secret_history(repository, base, base, [str(scanner)])

    assert result == 1
    assert "has no files to scan" in capsys.readouterr().out


def test_trusted_tree_uses_the_baseline_from_the_same_tree(tmp_path):
    repository, _base, scanner = _repository(tmp_path)
    (repository / ".secrets.baseline").write_text('{"current": true}\n', encoding="utf-8")
    (repository / "code.py").write_text("safe = True\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "trusted tree")

    result = _run_trusted(repository, "HEAD", scanner)

    assert result.returncode == 0
    assert "Trusted tree secret check passed" in result.stdout


def test_trusted_tree_rejects_scanner_failure(tmp_path):
    repository, _base, scanner = _repository(tmp_path)
    (repository / "secret.txt").write_text("DEMO_SECRET\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "secret")

    result = _run_trusted(repository, "HEAD", scanner)

    assert result.returncode == 1
    assert "Secret scan failed" in result.stdout


def test_secret_history_reports_git_failure(tmp_path):
    repository, _base, scanner = _repository(tmp_path)

    result = _run(repository, "missing-base", "HEAD", scanner)

    assert result.returncode == 2
    assert "preparation failed" in result.stdout
