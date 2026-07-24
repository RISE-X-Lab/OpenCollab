"""Static contracts for publication-critical GitHub workflows."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_hygiene_runs_for_pull_requests_and_main_pushes():
    workflow = _workflow("hygiene.yml")

    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "EMPTY_TREE=\"$(git hash-object -t tree /dev/null)\"" in workflow
    assert '"$EMPTY_TREE" "$HEAD_SHA" --require-files' in workflow
    assert "persist-credentials: false" in workflow


def test_conventional_title_checks_pr_title_and_pushed_commit_object():
    workflow = _workflow("lint-pr-title.yml")

    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "TITLE: ${{ github.event.pull_request.title }}" in workflow
    assert "COMMIT_SHA: ${{ github.sha }}" in workflow
    assert 'check_conventional_title.py --title "$TITLE"' in workflow
    assert 'check_conventional_title.py --commit "$COMMIT_SHA"' in workflow
    assert "persist-credentials: false" in workflow
