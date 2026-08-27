"""A handoff must not put one agent's files in another agent's diff.

Agents hand work over inside one repository: a coder commits in its own linked
worktree and sends the sha to a tester, which runs ``git checkout <sha>`` in
another linked worktree of the same repository (they share ``.git/objects``, so
nothing is fetched and no patch is applied). The tester then writes its own
files on top.

``get_diff`` used to measure every worktree against ``_base_commit``, the HEAD
pinned when that worktree was created. After a checkout that base is stale, so
the tester's diff carried the coder's files too — and the scheduler's
``worktree_changes`` record, which is the per-agent "who touched which file"
evidence, filed them all under the tester.

The base is now the commit HEAD was last moved *onto* rather than the one it
*grew from*, read out of the worktree's own HEAD reflog. An agent's own commits
therefore still count as its own work, and a worktree that never takes a handoff
is measured exactly where it was before.

These tests run real ``git`` against real linked worktrees. A mock cannot show
this: the whole failure lives in what git reports for a base that has moved.
"""

from __future__ import annotations

import subprocess

from opencollab.adapters.env import WorktreeEnvironment
from opencollab.application._scheduler_team import _parse_worktree_diff


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path):
    """A committed repository — the precondition for linked worktrees."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _changed_files(diff: str) -> list[str]:
    """What the scheduler's ``worktree_changes`` record would list."""
    return [path for path, _ in _parse_worktree_diff(diff)]


async def test_an_agents_own_commits_stay_in_its_own_diff(tmp_path) -> None:
    """Committing is how work is handed over, so it must not erase the record."""
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="diff-base-coder")
    try:
        await env.setup()
        creation_base = _git(env.workspace, "rev-parse", "HEAD")
        await env.write_file("f1.txt", "coder work\n")
        assert (await env.exec_cmd("git add -A && git commit -qm coder")).returncode == 0

        diff = await env.get_diff()

        assert _changed_files(diff) == ["f1.txt"]
        # HEAD moved, but it grew from the base rather than jumping to someone
        # else's commit, so the base did not move with it.
        assert _git(env.workspace, "rev-parse", "HEAD") != creation_base
        assert env.diff_base == creation_base
    finally:
        await env.cleanup()


async def test_a_checked_out_teammate_commit_is_not_this_agents_work(tmp_path) -> None:
    """The bug this exists for: the tester's row must not claim the coder's file."""
    source = _repo(tmp_path / "repo")
    coder = WorktreeEnvironment(str(source), branch_name="handoff-coder")
    tester = WorktreeEnvironment(str(source), branch_name="handoff-tester")
    try:
        await coder.setup()
        await coder.write_file("f1.txt", "coder work\n")
        assert (await coder.exec_cmd("git add -A && git commit -qm coder")).returncode == 0
        handoff_sha = _git(coder.workspace, "rev-parse", "HEAD")
        assert _changed_files(await coder.get_diff()) == ["f1.txt"]

        await tester.setup()
        # The handoff itself: same repository, shared object store, no fetch.
        assert (await tester.exec_cmd(f"git checkout -q {handoff_sha}")).returncode == 0
        await tester.write_file("f2.txt", "tester work\n")

        diff = await tester.get_diff()

        assert _changed_files(diff) == ["f2.txt"]
        assert "f1.txt" not in diff
        assert tester.diff_base == handoff_sha
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_work_committed_after_a_handoff_still_belongs_to_the_taker(tmp_path) -> None:
    """Adopting a base and then committing on it are different moves."""
    source = _repo(tmp_path / "repo")
    coder = WorktreeEnvironment(str(source), branch_name="after-coder")
    tester = WorktreeEnvironment(str(source), branch_name="after-tester")
    try:
        await coder.setup()
        await coder.write_file("f1.txt", "coder work\n")
        assert (await coder.exec_cmd("git add -A && git commit -qm coder")).returncode == 0
        handoff_sha = _git(coder.workspace, "rev-parse", "HEAD")

        await tester.setup()
        assert (await tester.exec_cmd(f"git checkout -q {handoff_sha}")).returncode == 0
        await tester.write_file("f2.txt", "tester work\n")
        assert (await tester.exec_cmd("git add -A && git commit -qm tester")).returncode == 0
        await tester.write_file("f3.txt", "still working\n")

        diff = await tester.get_diff()

        assert _changed_files(diff) == ["f2.txt", "f3.txt"]
        assert tester.diff_base == handoff_sha
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_a_worktree_that_never_takes_a_handoff_is_measured_where_it_was(
    tmp_path,
) -> None:
    """No checkout: the base is the creation base, exactly as before."""
    source = _repo(tmp_path / "repo")
    env = WorktreeEnvironment(str(source), branch_name="diff-base-plain")
    try:
        await env.setup()
        creation_base = _git(env.workspace, "rev-parse", "HEAD")
        await env.write_file("committed.txt", "committed\n")
        assert (await env.exec_cmd("git add -A && git commit -qm plain")).returncode == 0
        await env.write_file("untracked.txt", "untracked\n")
        await env.write_file("tracked.txt", "edited\n")

        diff = await env.get_diff()

        assert env.diff_base == creation_base
        assert _changed_files(diff) == ["committed.txt", "tracked.txt", "untracked.txt"]
    finally:
        await env.cleanup()


async def test_a_reset_onto_a_teammate_commit_moves_the_base_too(tmp_path) -> None:
    """``checkout`` is not the only way to adopt another agent's commit."""
    source = _repo(tmp_path / "repo")
    coder = WorktreeEnvironment(str(source), branch_name="reset-coder")
    tester = WorktreeEnvironment(str(source), branch_name="reset-tester")
    try:
        await coder.setup()
        await coder.write_file("f1.txt", "coder work\n")
        assert (await coder.exec_cmd("git add -A && git commit -qm coder")).returncode == 0
        handoff_sha = _git(coder.workspace, "rev-parse", "HEAD")

        await tester.setup()
        assert (await tester.exec_cmd(f"git reset -q --hard {handoff_sha}")).returncode == 0
        await tester.write_file("f2.txt", "tester work\n")

        assert _changed_files(await tester.get_diff()) == ["f2.txt"]
        assert tester.diff_base == handoff_sha
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_a_non_git_worktree_names_no_base_and_diffs_as_before(tmp_path) -> None:
    """The directory-copy fallback has no revisions; it must be untouched."""
    source = tmp_path / "plain"
    source.mkdir()
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    env = WorktreeEnvironment(str(source), branch_name="diff-base-copy")
    try:
        await env.setup()
        await env.write_file("added.txt", "added\n")

        diff = await env.get_diff()

        assert "added.txt" in diff
        assert env.diff_base is None
    finally:
        await env.cleanup()
