"""Regression tests for fail-closed patch extraction."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.git_patch import guarded_staged_diff_command


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt", ".gitignore")
    _git(repo, "commit", "-qm", "base")
    return repo


def _extract(
    repo: Path,
    *,
    exclude_paths: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        guarded_staged_diff_command(exclude_paths=exclude_paths),
        cwd=repo,
        env=env,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_patch_blocks_ignored_untracked_files_without_leaking_content(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    (repo / "secret.txt").write_text("api-token\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 125
    assert "ignored untracked files" in result.stderr
    assert "secret.txt" not in result.stdout
    assert "api-token" not in result.stdout
    assert _git(repo, "diff", "--cached", "--quiet").returncode == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diff.hide.command", "true"),
        ("diff.hide.textconv", "true"),
        ("filter.hide.clean", "true"),
        ("core.excludesfile", "/dev/null"),
        ("core.fsmonitor", "true"),
        ("core.sparsecheckout", "true"),
        ("core.sparsecheckoutcone", "true"),
        ("core.worktree", "/tmp/elsewhere"),
    ],
)
def test_patch_rejects_config_it_cannot_make_harmless(tmp_path, key, value):
    """Settings no command-line override can be trusted to defuse.

    A clean/smudge filter and an external diff driver are reached through a
    tracked ``.gitattributes`` this command does not control; a sparse
    checkout's skip-worktree bits outlive the setting that made them; a
    redirected working tree moves what is being read; and a monitor that is
    *on* can lie about what changed. Each makes the command exit 125 rather
    than report a patch it cannot vouch for.
    """
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "config", key, value)

    result = _extract(repo)

    assert result.returncode == 125
    assert "unsafe repository Git configuration" in result.stderr


def test_patch_rejects_repository_local_attributes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    info_attributes = repo / ".git" / "info" / "attributes"
    info_attributes.write_text("tracked.txt diff=hide\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 125
    assert "info/attributes" in result.stderr


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_patch_allows_attributes_that_only_turn_conversions_off(tmp_path):
    """The exact line the evaluation harness writes into its own snapshot.

    Every token unsets something -- line-ending conversion, clean/smudge
    filters, ``$Id$`` expansion, encoding conversion -- so the diff it produces
    is a more faithful reading of the bytes on disk, not a less faithful one.
    Refusing it left an agent in such a repository unable to produce any patch.
    """
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    info = repo / ".git" / "info" / "attributes"
    info.write_text("* -text -filter -ident -working-tree-encoding\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "+new" in result.stdout


def test_patch_rejects_attributes_that_mark_a_path_binary(tmp_path):
    """``-diff`` is the one unset that hides content instead of revealing it."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    info = repo / ".git" / "info" / "attributes"
    info.write_text("* -diff\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 125
    assert "info/attributes" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        "tracked.txt text",  # setting, not unsetting
        "tracked.txt working-tree-encoding=UTF-16",
        "[attr]hidden -diff",  # a macro definition
    ],
)
def test_patch_rejects_attributes_that_turn_anything_on(tmp_path, line):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    info = repo / ".git" / "info" / "attributes"
    info.write_text(f"{line}\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 125
    assert "info/attributes" in result.stderr


def test_patch_allows_a_comment_only_attributes_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    info = repo / ".git" / "info" / "attributes"
    info.write_text("# nothing to declare\n\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "+new" in result.stdout


def test_patch_rejects_non_regular_repository_attributes(tmp_path):
    repo = _repo(tmp_path)
    info_attributes = repo / ".git" / "info" / "attributes"
    os.mkfifo(info_attributes)

    result = _extract(repo)

    assert result.returncode == 125
    assert "not a regular file" in result.stderr


def test_patch_does_not_let_repository_exclude_rules_hide_candidates(tmp_path):
    repo = _repo(tmp_path)
    (repo / "payload.txt").write_text("candidate\n", encoding="utf-8")
    info_exclude = repo / ".git" / "info" / "exclude"
    info_exclude.write_text("# local excludes\n\npayload.txt\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "payload.txt" in result.stdout


def test_patch_allows_comment_only_repository_exclude_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "payload.txt").write_text("candidate\n", encoding="utf-8")
    info_exclude = repo / ".git" / "info" / "exclude"
    info_exclude.write_text("# local comments are harmless\n\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "payload.txt" in result.stdout


def test_patch_marks_controlled_workspace_safe_without_global_config(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"

    result = _extract(repo, env=env)

    assert result.returncode == 0, result.stderr
    assert "tracked.txt" in result.stdout


def test_patch_rejects_worktree_scoped_sparse_checkout(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.sparseCheckout", "true")

    result = _extract(repo)

    assert result.returncode == 125
    assert "core.sparsecheckout" in result.stderr


def test_patch_rejects_transform_config_loaded_through_include(tmp_path):
    repo = _repo(tmp_path)
    (repo / "payload.txt").write_text("candidate\n", encoding="utf-8")
    excludes = tmp_path / "external-excludes"
    excludes.write_text("payload.txt\n", encoding="utf-8")
    included = tmp_path / "included.gitconfig"
    included.write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
    _git(repo, "config", "include.path", str(included))

    result = _extract(repo)

    assert result.returncode == 125
    assert "core.excludesfile" in result.stderr


def test_patch_survives_repository_attributes_that_would_hide_content(tmp_path):
    """``core.attributesFile`` is pinned, so it cannot mark a file undiffable."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    attributes = tmp_path / "external-attributes"
    attributes.write_text("tracked.txt -diff\n", encoding="utf-8")
    _git(repo, "config", "core.attributesFile", str(attributes))

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "+new" in result.stdout
    assert "Binary files" not in result.stdout


def test_patch_survives_a_repository_that_hides_gitlink_changes(tmp_path):
    """``diff.ignoreSubmodules=all`` would drop a gitlink change from the patch."""
    repo = _repo(tmp_path)
    linked = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{linked},sub")
    _git(repo, "commit", "-qm", "add gitlink")
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "second")
    moved = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-index", "--cacheinfo", f"160000,{moved},sub")
    _git(repo, "config", "diff.ignoreSubmodules", "all")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "sub" in result.stdout


def test_patch_allows_a_repository_that_turns_its_monitor_off(tmp_path):
    """The exact local configuration the evaluation harness writes.

    Its snapshot sets these three to *disable* attribute and monitor influence
    on the tree it hashes. A guard that read the key name alone refused all
    three, so no agent working in such a repository could produce any patch at
    all -- the failure this case exists to keep from coming back.
    """
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "config", "core.attributesFile", os.devnull)
    _git(repo, "config", "core.fsmonitor", "false")
    _git(repo, "config", "diff.ignoreSubmodules", "all")

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "tracked.txt" in result.stdout
    assert "+new" in result.stdout


def test_patch_forces_filemode_evidence_on(tmp_path):
    repo = _repo(tmp_path)
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)
    _git(repo, "add", "script.sh")
    _git(repo, "commit", "-qm", "add script")
    _git(repo, "config", "core.filemode", "false")
    script.chmod(0o755)

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "old mode 100644" in result.stdout
    assert "new mode 100755" in result.stdout


def test_patch_ignores_replace_refs_when_reading_base_tree(tmp_path):
    repo = _repo(tmp_path)
    base_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "candidate tree")
    candidate_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", "-q", base_oid)
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "replace", base_oid, candidate_oid)

    result = _extract(repo)

    assert result.returncode == 0, result.stderr
    assert "-old" in result.stdout
    assert "+candidate" in result.stdout


def test_excluded_paths_do_not_modify_real_index(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    staged_before = _git(repo, "diff", "--cached", "--binary").stdout
    (repo / "excluded.txt").write_text("omit\n", encoding="utf-8")
    (repo / "included.txt").write_text("keep\n", encoding="utf-8")

    result = _extract(repo, exclude_paths=("excluded.txt",))

    assert result.returncode == 0, result.stderr
    assert "included.txt" in result.stdout
    assert "excluded.txt" not in result.stdout
    assert _git(repo, "diff", "--cached", "--binary").stdout == staged_before


def test_reserved_path_guard_reads_temporary_index(tmp_path):
    repo = _repo(tmp_path)
    retired = repo / ".opencollab-retired-candidate"
    retired.write_text("must not ship\n", encoding="utf-8")

    result = _extract(repo)

    assert result.returncode == 125
    assert "unregistered or modified .opencollab-retired-*" in result.stderr
    assert _git(repo, "diff", "--cached", "--quiet").returncode == 0
