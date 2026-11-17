from __future__ import annotations

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
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError
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
@pytest.mark.parametrize("change,sibling", [("rename", False), ("rename", True), ("readonly", True)])
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
        assert repo / "old/nested" in chmod_paths
        assert (repo / "old" in chmod_paths) is not sibling
    finally:
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
