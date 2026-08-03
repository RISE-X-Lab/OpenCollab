"""Repository-level checks for the public OpenCollab surface."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PUBLIC_TEXT_SUFFIXES = {
    ".css",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_PUBLIC_TEXT_NAMES = {
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "CODEOWNERS",
    "NOTICE",
}
_ACTION_REF = re.compile(r"^\s*(?:-\s+)?uses:\s+([^#\s]+)", re.MULTILINE)
_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_LOCALIZED_README = re.compile(r"README\.[a-z]{2}(?:-[A-Z]{2})?\.md")


def _repository_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        _REPO_ROOT / entry.decode()
        for entry in completed.stdout.split(b"\0")
        if entry and (_REPO_ROOT / entry.decode()).is_file()
    ]


def _public_text_files() -> list[Path]:
    return [
        path
        for path in _repository_files()
        if path.name != "LICENSE" and (path.suffix in _PUBLIC_TEXT_SUFFIXES or path.name in _PUBLIC_TEXT_NAMES)
    ]


def test_public_text_is_english_and_uses_canonical_project_names() -> None:
    stale_owner = "Yihong" + "Dong/OpenCollab"
    eval_name = "OpenCollab" + "-Eval"
    forbidden = (
        stale_owner,
        f"KaiEureka/{eval_name}",
        f"YihongDong/{eval_name}",
    )
    findings: list[str] = []

    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO_ROOT)
        for line_number, line in enumerate(text.splitlines(), start=1):
            contains_chinese = any("\u4e00" <= char <= "\u9fff" for char in line)
            if contains_chinese and not _LOCALIZED_README.fullmatch(path.name):
                findings.append(f"{relative}:{line_number}: non-English public text")
            for value in forbidden:
                if value in line:
                    findings.append(f"{relative}:{line_number}: stale public name {value!r}")

    assert not findings, "\n".join(findings)


def test_readme_points_to_the_canonical_evaluation_guide() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    eval_name = "OpenCollab" + "-Eval"
    canonical = f"https://github.com/RISE-X-Lab/{eval_name}#readme"

    assert f"[{eval_name} README]({canonical})" in readme


def test_distribution_metadata_points_to_the_canonical_repository() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    canonical = "https://github.com/RISE-X-Lab/OpenCollab"

    assert 'authors = [{ name = "OpenCollab contributors" }]' in pyproject
    assert 'maintainers = [{ name = "RISE-X-Lab" }]' in pyproject
    assert f'Homepage = "{canonical}"' in pyproject
    assert f'Repository = "{canonical}"' in pyproject
    assert f'Issues = "{canonical}/issues"' in pyproject


def test_secret_baseline_is_fully_audited() -> None:
    baseline = json.loads((_REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    findings = [finding for path_findings in baseline["results"].values() for finding in path_findings]

    assert baseline["version"] == "1.5.0"
    assert baseline["plugins_used"]
    assert findings
    assert all(finding.get("is_secret") is False for finding in findings)


def test_workflow_actions_are_immutable_and_checkout_drops_credentials() -> None:
    workflow_files = sorted((_REPO_ROOT / ".github/workflows").glob("*.yml"))
    assert workflow_files

    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        refs = _ACTION_REF.findall(text)
        for ref in refs:
            if ref.startswith("./"):
                continue
            _action, separator, revision = ref.partition("@")
            assert separator and _FULL_GIT_SHA.fullmatch(revision), (path.name, ref)

        checkout_count = text.count("actions/checkout@")
        assert text.count("persist-credentials: false") == checkout_count, path.name
        assert "\npermissions:\n  contents: read\n" in text, path.name
        assert "uv sync --extra dev" not in text, path.name
