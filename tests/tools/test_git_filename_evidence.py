"""Git change evidence preserves filenames across output modes."""

from __future__ import annotations

import subprocess

import pytest

from opencollab.application._scheduler_team import _parse_worktree_diff


@pytest.mark.parametrize(
    "name", ["trailing ", " leading", "both sides ", "unicode\u2028name", "tab\tname", "\u4e2d\u6587.txt"],
)
@pytest.mark.parametrize("kind", ["text", "binary", "rename"])
@pytest.mark.parametrize("quoted", ["true", "false"])
def test_git_evidence_preserves_the_actual_filename(tmp_path, name, kind, quoted):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (tmp_path / name).write_bytes(b"before\x00" if kind == "binary" else b"before\n")
    git("add", "-A")
    git("commit", "-qm", "initial")
    if kind == "rename":
        (tmp_path / name).rename(tmp_path / (name + " renamed "))
        git("add", "-A")
    else:
        (tmp_path / name).write_bytes(b"after\x00" if kind == "binary" else b"after\n")
    diff = git("-c", "core.quotePath=" + quoted, "diff", "-M", "HEAD")
    expected = [(name, "deleted"), (name + " renamed ", "added")] if kind == "rename" else [(name, "modified")]
    assert _parse_worktree_diff(diff) == expected
