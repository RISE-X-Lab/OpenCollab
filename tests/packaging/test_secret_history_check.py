"""Behavior tests for the proposed-history secret scan."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from tests.packaging.git_support import git as _git
from tests.support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT
_SCRIPT = _REPO_ROOT / "scripts" / "check_secret_history.py"


def _script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_secret_history", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repository(tmp_path: Path) -> tuple[Path, str, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Security Test")
    _git(repository, "config", "user.email", "security@example.invalid")
    audited = {
        "results": {
            "fixtures.py": [
                {"type": "Secret Keyword", "hashed_secret": "abc123", "line_number": 4}  # pragma: allowlist secret
            ]
        }
    }
    (repository / ".secrets.baseline").write_text(json.dumps(audited) + "\n", encoding="utf-8")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")

    scanner = tmp_path / "fake-scanner.py"
    scanner.write_text(
        """#!/usr/bin/env python3
import json
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
baseline = pathlib.Path(".secrets.baseline")
for name in paths:
    body = pathlib.Path(name).read_bytes()
    # A real detection: reported to the operator, baseline left untouched.
    if b"DEMO_SECRET" in body:
        raise SystemExit(1)
    if b"DEMO_CORRUPT_BASELINE" in body:
        baseline.write_text("not json", encoding="utf-8")
        raise SystemExit(3)
    # detect-secrets rewrites the baseline and exits 3 once anything about a
    # recorded entry no longer matches the tree it is scanning.
    if b"DEMO_MOVED" in body or b"DEMO_UNAUDITED" in body:
        document = json.loads(baseline.read_text(encoding="utf-8"))
        entries = document["results"]["fixtures.py"]
        entries[0]["line_number"] += 10
        if b"DEMO_UNAUDITED" in body:
            entries.append(
                {"type": "AWS Access Key", "hashed_secret": "def456", "line_number": 9}  # pragma: allowlist secret
            )
        baseline.write_text(json.dumps(document), encoding="utf-8")
        raise SystemExit(3)
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


def test_secret_history_accepts_an_audited_secret_that_only_moved(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / "fixtures.py").write_text("DEMO_MOVED\n", encoding="utf-8")
    _git(repository, "add", "fixtures.py")
    _git(repository, "commit", "-m", "shift an audited fixture down its file")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 0
    assert "only moved already-audited secrets" in result.stdout


def test_secret_history_rejects_a_secret_absent_from_the_trusted_baseline(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / "fixtures.py").write_text("DEMO_UNAUDITED\n", encoding="utf-8")
    _git(repository, "add", "fixtures.py")
    _git(repository, "commit", "-m", "record an unaudited secret")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 3
    assert "AWS Access Key is not present in the trusted baseline" in result.stdout
    assert "Secret scan failed for proposed commit" in result.stdout


def test_secret_history_rejects_a_baseline_it_cannot_read_after_scanning(tmp_path):
    repository, base, scanner = _repository(tmp_path)
    (repository / "fixtures.py").write_text("DEMO_CORRUPT_BASELINE\n", encoding="utf-8")
    _git(repository, "add", "fixtures.py")
    _git(repository, "commit", "-m", "leave an unreadable baseline behind")

    result = _run(repository, base, "HEAD", scanner)

    assert result.returncode == 3
    assert "Unreadable baseline" in result.stdout


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


def test_secret_history_rejects_zero_file_scan(tmp_path, monkeypatch, capsys):
    repository, base, scanner = _repository(tmp_path)
    module = _script_module()
    monkeypatch.setattr(module, "_scannable_blobs", lambda *_args: [])

    result = module.check_secret_history(repository, base, base, [str(scanner)])

    assert result == 1
    assert "has no files to scan" in capsys.readouterr().out


def test_secret_history_scans_each_path_and_blob_pair_once(tmp_path, monkeypatch):
    repository, base, scanner = _repository(tmp_path)
    (repository / "first.txt").write_text("first version\n", encoding="utf-8")
    _git(repository, "add", "first.txt")
    _git(repository, "commit", "-m", "add first file")
    (repository / "second.txt").write_text("second version\n", encoding="utf-8")
    _git(repository, "add", "second.txt")
    _git(repository, "commit", "-m", "add second file")

    module = _script_module()
    scanned_batches: list[list[tuple[str, str]]] = []

    def record_scan(
        _repository: Path,
        _commit: str,
        blobs: list[tuple[str, str]],
        _scanner: list[str],
        _trusted_baseline: bytes,
        _root: Path,
    ) -> int:
        scanned_batches.append(blobs)
        return 0

    monkeypatch.setattr(module, "_scan_materialized_blobs", record_scan)

    result = module.check_secret_history(repository, base, "HEAD", [str(scanner)])

    assert result == 0
    scanned_paths = [path for batch in scanned_batches for path, _object_id in batch]
    assert scanned_paths == ["first.txt", "second.txt"]


def test_secret_history_rescans_same_blob_at_a_new_path(tmp_path, monkeypatch):
    repository, base, scanner = _repository(tmp_path)
    (repository / "first.txt").write_text("shared contents\n", encoding="utf-8")
    _git(repository, "add", "first.txt")
    _git(repository, "commit", "-m", "add first path")
    _git(repository, "mv", "first.txt", "second.txt")
    _git(repository, "commit", "-m", "rename path")

    module = _script_module()
    scanned_batches: list[list[tuple[str, str]]] = []

    def record_scan(
        _repository: Path,
        _commit: str,
        blobs: list[tuple[str, str]],
        _scanner: list[str],
        _trusted_baseline: bytes,
        _root: Path,
    ) -> int:
        scanned_batches.append(blobs)
        return 0

    monkeypatch.setattr(module, "_scan_materialized_blobs", record_scan)

    result = module.check_secret_history(repository, base, "HEAD", [str(scanner)])

    assert result == 0
    scanned = [(path, object_id) for batch in scanned_batches for path, object_id in batch]
    assert [path for path, _object_id in scanned] == ["first.txt", "second.txt"]
    assert scanned[0][1] == scanned[1][1]


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
