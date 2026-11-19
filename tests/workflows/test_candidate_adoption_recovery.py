from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._candidate_increment import _apply_increment
from opencollab.adapters._env_base import ExecResult
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError
from opencollab.workflows import CandidateRun
from tests.workflows.test_workflow_candidate_workspace import _git


def _run(repo, *, cache=False):
    return subprocess.run(
        [sys.executable, *([] if cache else ["-B"]), "app.py"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout


def _contents(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        mode = path.lstat().st_mode
        result[str(relative)] = (stat.S_IFMT(mode), stat.S_IMODE(mode),
                                 os.readlink(path) if path.is_symlink() else
                                 path.read_bytes() if path.is_file() else None)
    return result


def _repository(tmp_path, *, physical=False, cache=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "settings").mkdir()
    (repo / "settings/defaults.py").write_text("VALUE = 1\n")
    (repo / "loader.py").write_text("from settings.defaults import VALUE\ndef value():\n    return VALUE\n")
    (repo / "app.py").write_text("from loader import value\nprint(value())\n")
    (repo / "other.py").write_text("VALUE = 1\n")
    (repo / "note.txt").write_text("committed\n")
    (repo / ".gitignore").write_text("__pycache__/\nignored-link\n")
    if physical:
        (repo / ".gitattributes").write_text("*.py text eol=crlf\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare candidate recovery")
    for name in ("note.txt", "loader.py"):
        with (repo / name).open("a") as stream:
            stream.write("# staged user change\n")
        _git(repo, "add", name)
        with (repo / name).open("a") as stream:
            stream.write("# unstaged user change\n")
    (repo / "draft.txt").write_text("untracked user draft\n")
    (repo / "draft.txt").chmod(0o600)
    (repo / "loader.py").chmod(0o750)
    (repo / "settings").chmod(0o750)
    (repo / "settings/defaults.py").chmod(0o640)
    assert _run(repo, cache=cache) == "1\n"
    if cache:
        (repo / "settings/ignored-link").symlink_to("../../outside-user-file")
        assert list((repo / "settings/__pycache__").glob("*.pyc"))
    return repo


def _flatten(folder):
    (folder / "settings/defaults.py").unlink()
    (folder / "settings").rmdir()
    (folder / "settings").write_text("42\n")
    (folder / "loader.py").write_text(
        'from pathlib import Path\ndef value():\n    return int(Path("settings").read_text())\n'
    )
    (folder / "draft.txt").write_text("candidate draft\n")


def _partial_publication(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "record.txt").write_text("original\n")
    _git(repo, "add", "record.txt")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare partial publication")
    (repo / "settings").mkdir()
    (repo / "settings/notes.txt").write_text("existing settings\n")
    patch = (
        "diff --git a/record.txt b/record.txt\n--- a/record.txt\n+++ b/record.txt\n"
        "@@ -1 +1 @@\n-original\n+candidate\n"
        "diff --git a/settings b/settings\nnew file mode 100644\n--- /dev/null\n+++ b/settings\n"
        "@@ -0,0 +1 @@\n+replacement settings\n"
    )
    return repo, CandidateRun("record update", None, patch, (), ())


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("cache", [False, True])
async def test_public_sdk_candidate_recovers_directory_replacement(tmp_path, physical, cache):
    repo = _repository(tmp_path, physical=physical, cache=cache)
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()

    async def edit(child, inputs):
        _flatten(Path(child.workspace_root))
        assert _run(Path(child.workspace_root)) == "42\n"
        return "tested"

    async def flow(ctx, inputs):
        candidate = await ctx.candidate_workflow(edit, {}, label="flatten")
        assert "+42" in candidate.effective_diff
        if cache:
            with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
                await ctx.adopt_candidate(candidate)
            assert _contents(repo) == before
        else:
            await ctx.adopt_candidate(candidate)
        return "finished"

    result = await OpenCollab(repo).workflow(flow, trace=False)
    assert result.ok, result.reason
    assert _run(repo) == ("1\n" if cache else "42\n")
    assert (repo / ".git/index").read_bytes() == index
    assert not list((repo / ".git").glob("candidate-recovery-*"))
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("pruned", ["both", "nested", "none"])
@pytest.mark.parametrize("cache", [False, True])
async def test_public_sdk_recovers_pruned_ancestors_and_modes(tmp_path, physical, pruned, cache):
    repo = _repository(tmp_path, physical=physical, cache=cache)
    (repo / "old/nested").mkdir(parents=True)
    (repo / "old/nested/helper.py").write_text("VALUE = 1\n")
    _git(repo, "add", "old/nested/helper.py")
    (repo / "old").chmod(0o750)
    (repo / "old/nested").chmod(0o710)
    (repo / "old/nested/helper.py").chmod(0o640)
    if pruned != "both":
        sibling = "old/sibling.txt" if pruned == "nested" else "old/nested/sibling.txt"
        (repo / sibling).write_text("user sibling\n")
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))

    async def edit(child, inputs):
        folder = Path(child.workspace_root)
        (folder / "old/nested/helper.py").unlink()
        _flatten(folder)
        assert _run(folder) == "42\n"
        return "tested"

    async def flow(ctx, inputs):
        candidate = await ctx.candidate_workflow(edit, {}, label="pruned-parent")
        if cache:
            with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
                await ctx.adopt_candidate(candidate)
            assert _contents(repo) == before
        else:
            await ctx.adopt_candidate(candidate)
            assert not (repo / "old/nested/helper.py").exists()
            assert (repo / "old").exists() is (pruned != "both")
            assert (repo / "old/nested").exists() is (pruned == "none")
        assert not environment.revoked
        assert (await environment.exec_cmd("printf recovered")).stdout == "recovered"
        return "recovered"

    try:
        result = await OpenCollab(repo, environment=environment).workflow(flow, trace=False)
        assert result.ok, result.reason
        assert result.output == "recovered"
        assert _run(repo) == ("1\n" if cache else "42\n")
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("change,sibling", [
    ("rename", False), ("rename", True), ("readonly", True), ("readonly-root", True),
])
async def test_public_sdk_restores_changed_ancestor_permissions(tmp_path, monkeypatch, physical, change, sibling):
    repo = _repository(tmp_path, physical=physical, cache=True)
    (repo / "old/nested").mkdir(parents=True)
    helper = repo / "old/nested/helper.py"
    helper.write_text("VALUE = 1\n")
    _git(repo, "add", "old/nested/helper.py")
    (repo / "old").chmod(0o750)
    (repo / "old/nested").chmod(0o500 if change == "readonly" else 0o710)
    if sibling:
        (repo / "old/sibling.txt").write_text("shared contents\n")
    if change == "readonly-root":
        repo.chmod(0o500)
    root_mode = stat.S_IMODE(repo.stat().st_mode)
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "chmod.log"
    chmod = shutil.which("chmod")
    assert chmod is not None
    (commands / "chmod").write_text(
        f'#!/bin/sh\nprintf \'%s\\0\' "$@" >> {shlex.quote(str(log))}\n'
        f'exec {shlex.quote(chmod)} "$@"\n'
    )
    (commands / "chmod").chmod(0o755)
    execute = environment.exec_cmd

    async def recorded(command, timeout=120.0, **kwargs):
        if command.startswith("failed=0;"):
            command = f"PATH={shlex.quote(str(commands) + os.pathsep + os.environ['PATH'])}; " + command
        return await execute(command, timeout, **kwargs)

    monkeypatch.setattr(environment, "exec_cmd", recorded)

    async def edit(child, inputs):
        folder = Path(child.workspace_root)
        if change == "rename":
            (folder / "old/nested/helper.py").rename(folder / "old/nested/renamed.py")
        else:
            (folder / "old/nested/helper.py").write_text("VALUE = 42\n")
        _flatten(folder)
        assert _run(folder) == "42\n"
        return "tested"

    async def flow(ctx, inputs):
        candidate = await ctx.candidate_workflow(edit, {}, label="ancestor-permissions")
        if change == "rename":
            assert "rename from old/nested/helper.py" in candidate.effective_diff
        with pytest.raises(RuntimeError, match="Directory not empty|Permission denied|Is a directory"):
            await ctx.adopt_candidate(candidate)
        assert _contents(repo) == before
        assert not environment.revoked
        return "recovered"

    try:
        result = await OpenCollab(repo, environment=environment).workflow(flow, trace=False)
        assert result.ok, result.reason
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        chmod_paths = [Path(value.decode()) for value in log.read_bytes().split(b"\0")[2::3] if value]
        assert stat.S_IMODE(repo.stat().st_mode) == root_mode
        assert (repo / "old/nested" in chmod_paths) is (change != "readonly-root")
        assert (repo / "old" in chmod_paths) is not sibling
        assert (repo in chmod_paths) is (change == "readonly-root")
    finally:
        repo.chmod(0o700)
        (repo / "old/nested").chmod(0o700)
        await environment.cleanup()


async def test_git_write_failure_after_successful_check_restores_partial_application(tmp_path):
    repo = _repository(tmp_path)
    (repo / "z").mkdir(mode=0o555)
    if os.access(repo / "z", os.W_OK):
        pytest.skip("requires directory write permissions to be enforced")
    patch = (
        "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
        "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 42\n"
        "diff --git a/z/new.txt b/z/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/z/new.txt\n@@ -0,0 +1 @@\n+new contents\n"
    )
    checked = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check"], input=patch, capture_output=True, text=True,
    )
    assert checked.returncode == 0, checked.stderr
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    try:
        with pytest.raises(RuntimeError, match="unable to write file|Permission denied"):
            await EnvCandidateWorkspace(environment).adopt(patch)
        assert _contents(repo) == before
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment.revoked
        assert (await environment.exec_cmd("printf usable")).stdout == "usable"
    finally:
        (repo / "z").chmod(0o755)
        await environment.cleanup()


@pytest.mark.parametrize(("rules", "blocked"), [
    ("tracked", True),
    ("unstaged", True),
    ("nested", True),
    ("info", True),
    ("configured", True),
    ("tracked", False),
])
async def test_public_sdk_replays_effective_crlf_attributes(tmp_path, rules, blocked):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "nested").mkdir()
    target = repo / "nested/a.txt"
    target.write_bytes(b"original\n")
    attributes = {
        "nested": repo / "nested/.gitattributes",
        "info": repo / ".git/info/attributes",
        "configured": repo / "local-attributes",
    }.get(rules, repo / ".gitattributes")
    if rules == "configured":
        _git(repo, "config", "core.attributesFile", attributes.name)
    if rules in {"tracked", "unstaged"}:
        eol = "lf" if rules == "unstaged" else "crlf"
        attributes.write_text(f"*.txt text eol={eol}\n")
        _git(repo, "add", ".gitattributes")
    _git(repo, "add", "nested/a.txt")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare CRLF recovery")
    if rules != "tracked":
        attributes.write_text("*.txt text eol=crlf\n")
    target.write_bytes(b"original\r\n")
    patch = (
        "diff --git a/nested/a.txt b/nested/a.txt\n--- a/nested/a.txt\n+++ b/nested/a.txt\n"
        "@@ -1 +1 @@\n-original\n+candidate\n"
    )
    if blocked:
        (repo / "settings").mkdir()
        (repo / "settings/cache.txt").write_text("preserve cache\n")
        patch += (
            "diff --git a/settings b/settings\nnew file mode 100644\n--- /dev/null\n+++ b/settings\n"
            "@@ -0,0 +1 @@\n+new settings\n"
        )
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    publications = []
    replays = []
    change_rules = rules in {"nested", "configured"}

    async def observe(command, timeout=120.0, **kwargs):
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command:
            publications.append((result.returncode, target.read_bytes()))
            if change_rules:
                attributes.write_text("*.txt text eol=lf\n")
        if "candidate-recovery-" in command and " apply " in command:
            replays.append(result.returncode)
        return result

    environment.exec_cmd = observe

    async def flow(ctx, inputs):
        candidate = CandidateRun("text update", "ready", patch, (), ())
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(flow, trace=False)
        assert publications == [(128 if blocked else 0, b"candidate\r\n")]
        if blocked:
            assert type(result.error) is RuntimeError
            assert "Directory not empty" in result.reason
            assert replays == [0, 128]
            assert target.read_bytes() == b"original\r\n"
            assert (repo / "settings/cache.txt").read_bytes() == b"preserve cache\n"
        else:
            assert result.ok, result.reason
            assert target.read_bytes() == b"candidate\r\n"
            assert replays == []
        assert attributes.read_text() == f"*.txt text eol={'lf' if change_rules else 'crlf'}\n"
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment.revoked
    finally:
        await environment.cleanup()


@pytest.mark.parametrize(("replay_status", "target_kind"), [
    (1, "existing"), (128, "existing"), (128, "new"), (128, "sibling"),
])
async def test_unverified_candidate_replay_preserves_live_entries(tmp_path, replay_status, target_kind):
    repo = _repository(tmp_path)
    if target_kind == "existing":
        target = repo / "other.py"
        patch = (
            "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
            "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 42\n"
        )
    else:
        target = repo / "new/inner/file.py"
        patch = (
            "diff --git a/new/inner/file.py b/new/inner/file.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/new/inner/file.py\n@@ -0,0 +1 @@\n+VALUE = 42\n"
        )
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd

    async def fault(command, timeout=120.0, **kwargs):
        if " apply --whitespace=nowarn --binary " in command:
            return ExecResult(replay_status, "", "replay interrupted before writing")
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command:
            assert result.returncode == 0, result.stderr
            if target_kind == "sibling":
                target.unlink()
                target.with_name("notes.txt").write_text("unrelated notes\n")
            raise OSError("publication receipt unavailable")
        return result

    environment.exec_cmd = fault
    try:
        if target_kind == "sibling":
            with pytest.raises(OSError, match="publication receipt unavailable"):
                await _apply_increment(environment, str(repo), patch)
            assert target.with_name("notes.txt").read_text() == "unrelated notes\n"
            assert not target.exists()
            assert not list((repo / ".git").glob("candidate-recovery-*"))
            assert not environment.revoked
        else:
            with pytest.raises(CandidateAdoptionRecoveryError, match="original files retained"):
                await _apply_increment(environment, str(repo), patch)
            assert target.read_text() == "VALUE = 42\n"
            backup, = (repo / ".git").glob("candidate-recovery-*")
            record, = backup.glob("*.path")
            if target_kind == "existing":
                assert record.with_suffix("").read_bytes() == b"VALUE = 1\n"
            else:
                assert Path(record.read_text()) == repo / "new"
                assert not record.with_suffix("").exists()
            assert environment.revoked
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("inside_repository", [False, True])
async def test_directory_copy_candidate_replays_partial_apply_outside_git(tmp_path, inside_repository):
    if inside_repository:
        _git(tmp_path, "init", "-q")
    repo = tmp_path / "copy"
    (repo / "settings").mkdir(parents=True)
    (repo / "settings/retained.txt").write_text("keep\n")
    (repo / "a.txt").write_text("original\n")
    patch = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-original\n+candidate\n"
        "diff --git a/settings b/settings\nnew file mode 100644\n--- /dev/null\n+++ b/settings\n"
        "@@ -0,0 +1 @@\n+candidate settings\n"
    )
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    intermediate = []

    async def inspect(command, timeout=120.0, **kwargs):
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command:
            intermediate.append((repo / "a.txt").read_text())
        return result

    environment.exec_cmd = inspect
    try:
        with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
            await _apply_increment(environment, str(repo), patch, repository=False)
        assert intermediate == ["candidate\n"]
        assert (repo / "a.txt").read_text() == "original\n"
        assert (repo / "settings/retained.txt").read_text() == "keep\n"
        assert not environment.revoked
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("replay_full", [False, True])
async def test_recovery_preserves_saved_entries_when_copies_fail(tmp_path, replay_full):
    repo = _repository(tmp_path)
    target = repo / "other.py"
    patch = (
        "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
        "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 42\n"
    )
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    failures = []

    async def fault(command, timeout=120.0, **kwargs):
        if command.startswith("present()") and replay_full:
            failures.append("replay")
            raise OSError(errno.ENOSPC, "No space left on device")
        if command.startswith("failed=0;"):
            command = "cp() { echo 'restore copy: No space left on device' >&2; return 1; }; " + command
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command and ".candidate-adopt-" in command:
            assert result.returncode == 0, result.stderr
            assert target.read_text() == "VALUE = 42\n"
            if replay_full:
                target.write_text("VALUE = 100\n")
            failures.append("receipt")
            raise OSError("publication receipt unavailable")
        return result

    environment.exec_cmd = fault
    try:
        if replay_full:
            detail = "No space left on device.*original files retained"
            with pytest.raises(CandidateAdoptionRecoveryError, match=detail):
                await _apply_increment(environment, str(repo), patch)
            backup, = (repo / ".git").glob("candidate-recovery-*")
            record, = backup.glob("*.path")
            assert record.with_suffix("").read_text() == "VALUE = 1\n"
            assert target.read_text() == "VALUE = 100\n"
            assert failures == ["receipt", "replay"]
        else:
            with pytest.raises(OSError, match="publication receipt unavailable"):
                await _apply_increment(environment, str(repo), patch)
            assert target.read_text() == "VALUE = 1\n"
            assert not list((repo / ".git").glob("candidate-recovery-*"))
            assert failures == ["receipt"]
        assert environment.revoked is replay_full
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()


async def test_recovery_preserves_a_write_after_comparing_the_live_file(tmp_path):
    repo = _repository(tmp_path)
    target = repo / "other.py"
    marker = tmp_path / "compared"
    patch = (
        "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
        "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 42\n"
    )
    index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd

    async def after_comparison(command, timeout=120.0, **kwargs):
        if command.startswith("failed=0;"):
            command = (
                'cmp() { command cmp "$@" || return; '
                f'if [ "$3" = {shlex.quote(str(target))} ] && [ ! -e {shlex.quote(str(marker))} ]; then '
                f"printf 'VALUE = 100\\n' > {shlex.quote(str(target))} && touch {shlex.quote(str(marker))}; "
                "fi; }; " + command
            )
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command and ".candidate-adopt-" in command:
            assert result.returncode == 0, result.stderr
            raise OSError("publication receipt unavailable")
        return result

    environment.exec_cmd = after_comparison
    try:
        with pytest.raises((OSError, CandidateAdoptionRecoveryError)) as failed:
            await _apply_increment(environment, str(repo), patch)
        assert marker.is_file()
        assert target.read_text() == "VALUE = 100\n"
        assert isinstance(failed.value, CandidateAdoptionRecoveryError)
        assert "concurrent change at " + str(target) in str(failed.value)
        backup, = (repo / ".git").glob("candidate-recovery-*")
        record, = backup.glob("*.path")
        assert record.with_suffix("").read_text() == "VALUE = 1\n"
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("linked", [False, True])
async def test_recovery_compares_parked_bytes_before_removing_the_backup(tmp_path, linked):
    source, candidate = _partial_publication(tmp_path)
    repo = source
    if linked:
        repo = tmp_path / "linked"
        _git(source, "worktree", "add", "--detach", str(repo))
        (repo / "settings").mkdir()
        (repo / "settings/notes.txt").write_text("existing settings\n")
    target = repo / "record.txt"
    original = target.read_bytes()
    index = Path(_git(repo, "rev-parse", "--absolute-git-dir").removesuffix("\n")) / "index"
    index_before = index.read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    writer = None
    appended = b"# independent write\n"
    published = None

    async def observe(command, timeout=120.0, **kwargs):
        nonlocal writer, published
        if command.startswith("recovery="):
            assert writer is not None
            assert target.read_bytes() == original
            writer.write(appended)
            writer.flush()
            os.fsync(writer.fileno())
            writer.close()
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command and ".candidate-adopt-" in command:
            assert result.returncode == 128, result.stderr
            published = target.read_bytes()
            assert published != original
            writer = target.open("ab")
        return result

    environment.exec_cmd = observe

    async def flow(ctx, _):
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(flow, trace=False)
        assert isinstance(result.error, CandidateAdoptionRecoveryError)
        assert f"concurrent change at {target}" in result.reason
        backup, = (source / ".git").glob("candidate-recovery-*")
        assert str(backup) in result.reason
        record, = (record for record in backup.glob("observed-*/path") if record.read_text() == str(target))
        assert record.with_name("entry").read_bytes() == published + appended
        assert target.read_bytes() == original
        assert index.read_bytes() == index_before
    finally:
        if writer is not None:
            writer.close()
        await environment.cleanup()
    if linked:
        _git(source, "worktree", "remove", "--force", str(repo))
        assert not index.exists()
        assert record.with_name("entry").read_bytes() == published + appended


@pytest.mark.parametrize("path", [".", ".gi\u200ct/config"])
async def test_git_rejects_metadata_aliases_before_candidate_backup(tmp_path, path):
    repo = _repository(tmp_path)
    _git(repo, "config", "core.protectHFS", "true")
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    patch = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
             f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+unsafe\n")
    environment = LocalEnvironment(str(repo))
    try:
        with pytest.raises(RuntimeError, match="invalid path"):
            await EnvCandidateWorkspace(environment).adopt(patch)
        assert _contents(repo) == before
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment.revoked
    finally:
        await environment.cleanup()


class _PublishingFailureEnvironment(LocalEnvironment):
    def __init__(self, workspace, *, recovery_failure=False, publication_failure=True):
        super().__init__(str(workspace))
        self.recovery_failure = recovery_failure
        self.publication_failure = publication_failure
        self.published = False

    async def exec_cmd(self, command, timeout=120.0, **kwargs):
        if command.startswith("failed=0;") and self.recovery_failure:
            raise OSError("recovery device unavailable")
        result = await super().exec_cmd(command, timeout, **kwargs)
        if (self.publication_failure and " apply --binary " in command
                and ".candidate-adopt-" in command and result.returncode == 0):
            self.published = True
            raise OSError("publication receipt I/O failure")
        return result


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("recover", [False, True])
async def test_publication_error_restores_types_permissions_and_new_parents(tmp_path, physical, recover):
    repo = _repository(tmp_path, physical=physical)
    (repo / "link").symlink_to("draft.txt")
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    environment = _PublishingFailureEnvironment(repo, recovery_failure=not recover)

    async def edit(child, inputs):
        folder = Path(child.workspace_root)
        _flatten(folder)
        (folder / "created/nested").mkdir(parents=True)
        (folder / "created/nested/new.txt").write_text("new contents\n")
        (folder / "link").unlink()
        (folder / "link").write_text("regular replacement\n")
        return "ready"

    async def flow(ctx, inputs):
        candidate = await ctx.candidate_workflow(edit, {}, label="publication")
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment).workflow(flow, trace=False)
        assert environment.published
        assert not result.ok
        if recover:
            assert isinstance(result.error, OSError)
            assert str(result.error) == "publication receipt I/O failure"
            assert _contents(repo) == before
            assert _run(repo) == "1\n"
            assert not environment.revoked
        else:
            assert isinstance(result.error, CandidateAdoptionRecoveryError)
            assert "publication receipt I/O failure" in result.reason
            assert "recovery device unavailable" in result.reason
            retained, = (repo / ".git").glob("candidate-recovery-*")
            assert str(retained) in result.reason
            originals = {
                Path(path.read_text()).relative_to(repo).as_posix(): path.with_suffix("")
                for path in retained.glob("*.path")
            }
            assert originals["loader.py"].read_bytes() == before["loader.py"][2]
            assert originals["loader.py"].stat().st_mode & 0o777 == 0o750
            assert originals["settings"].is_dir()
            assert originals["link"].is_symlink()
            assert os.readlink(originals["link"]) == "draft.txt"
            assert environment.revoked
            with pytest.raises(RuntimeError, match="aborted"):
                await environment.exec_cmd("printf unexpected > other.py")
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()


def _response(name, arguments, role, step):
    return LLMResponse(tool_calls=[{
        "id": f"{role}-{step}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }], finish_reason="tool_calls", usage=Usage(1, 1))


class _DuoModel:
    def __init__(self, *, independent):
        self.independent = independent

    async def complete(self, messages, **kwargs):
        prompt = "\n".join(str(message.get("content") or "") for message in messages
                           if message.get("role") == "user")
        role = "A" if "You are candidate A," in prompt else "B" if "You are candidate B," in prompt else "judge"
        step = sum(message.get("role") == "tool" for message in messages)
        if role in {"A", "B"}:
            if step == 0:
                if role == "A":
                    script = (
                        'from pathlib import Path; p=Path("settings"); (p/"defaults.py").unlink(); '
                        'p.rmdir(); p.write_text("42\\n"); '
                        'Path("loader.py").write_text("from pathlib import Path\\ndef value():\\n'
                        '    return int(Path(\\"settings\\").read_text())\\n")'
                    )
                else:
                    path = "other.py" if self.independent else "settings/defaults.py"
                    script = f"from pathlib import Path; Path({path!r}).write_text('VALUE = 42\\n')"
                return _response("bash", {"command": shlex.join([sys.executable, "-B", "-c", script])}, role, step)
            if step == 1:
                return _response("bash", {"command": shlex.join([sys.executable, "-B", "app.py"])}, role, step)
            return LLMResponse(content=f"Candidate {role} finished", usage=Usage(1, 1))
        return _response("structured_output", {
            "winner": "A", "requirements_complete": True,
            "requirements": [{
                "requirement": "Read a flat settings file and print 42",
                "a_coverage": "covered", "b_coverage": "not_covered",
                "a_evidence": ["loader.py reads the flat settings file containing 42"],
                "b_evidence": ["settings/defaults.py still uses the original directory layout"],
            }],
            "rationale": "A replaces settings/defaults.py with settings and updates loader.py",
        }, role, step)


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("recovery_failure", [False, True])
async def test_duo_fallback_requires_recovered_source(tmp_path, independent, recovery_failure):
    repo = _repository(tmp_path, cache=True)
    before = _contents(repo)
    index = (repo / ".git/index").read_bytes()
    environment = _PublishingFailureEnvironment(
        repo, recovery_failure=recovery_failure, publication_failure=False,
    )
    backend = EnvCandidateWorkspace(environment)
    attempted = []
    adopt = backend.adopt_run

    async def recorded(candidate, preserve_paths=()):
        attempted.append(candidate.label)
        await adopt(candidate, preserve_paths)

    backend.adopt_run = recorded
    try:
        result = await OpenCollab(repo, environment=environment, model="local-scripted").workflow(
            "duo", {"goal": "Read a flat settings file and print 42", "allow_unisolated_shell": True,
                    "candidate_evidence_dir": str(tmp_path / "evidence")},
            llm=_DuoModel(independent=independent), candidate_workspace=backend,
            trace=False, max_steps=5, budget=10000, limit_mode="explicit", timeout=30,
        )
        if recovery_failure:
            assert not result.ok
            assert isinstance(result.error, CandidateAdoptionRecoveryError)
            assert len(attempted) == 1
            assert "Directory not empty" in result.reason
            assert "original files retained" in result.reason
            assert list((repo / ".git").glob("candidate-recovery-*"))
        else:
            assert result.ok, result.reason
            assert len(attempted) == 2
            assert result.output["status"] == "done"
            assert result.output["adopted"] == "B"
            target = "other.py" if independent else "settings/defaults.py"
            after = _contents(repo)
            assert after.pop(target)[2] == b"VALUE = 42\n"
            before.pop(target)
            assert after == before
            assert _run(repo) == ("1\n" if independent else "42\n")
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()
