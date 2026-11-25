from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.tool_execution import ToolRuntime
from tests.runtime.test_team_candidate_delivery import _adoption_scheduler
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


@pytest.mark.parametrize("backend", ["workflow", "team"])
@pytest.mark.parametrize("change", [
    "new-script", "empty-canonical", "local-context", "overlap", "binary", "filter", "encoding",
])
async def test_candidate_delivery_keeps_tested_physical_contents(tmp_path, backend, change):
    repo = _repository(tmp_path)
    attributes = "*.sh text eol=crlf\n*.txt text eol=crlf\n*.bin text eol=crlf\n"
    if change == "filter":
        script = tmp_path / "filter.py"
        script.write_text(
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(data.removeprefix(b'WORK:') if sys.argv[1] == 'clean' else b'WORK:' + data)\n"
        )
        for mode in ("clean", "smudge"):
            _git(repo, "config", f"filter.work.{mode}", shlex.join((sys.executable, str(script), mode)))
        attributes += "*.filtered filter=work\n"
        (repo / "data.filtered").write_bytes(b"WORK:original\n")
        if backend == "team":
            pytest.skip("Team candidates reject repository-local filters before execution")
    if change == "encoding":
        attributes += "*.utf16 working-tree-encoding=UTF-16\n"
        (repo / "data.utf16").write_bytes("original\n".encode("utf-16"))
    (repo / ".gitattributes").write_text(attributes)
    script_contents = b'#!/bin/sh\nprintf "42\\n"\n'
    if change == "empty-canonical":
        (repo / "run.sh").write_bytes(script_contents.replace(b"\n", b"\r\n"))
        (repo / "run.sh").chmod(0o755)
    text = b"".join(f"line {number}\r\n".encode() for number in range(60))
    (repo / "data.txt").write_bytes(text)
    (repo / "data.bin").write_bytes(b"original\0\xff\r\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Prepare physical candidate delivery")
    _git(repo, "config", "core.filemode", "false")
    original_index = (repo / ".git/index").read_bytes()
    initial_text = (repo / "data.txt").read_bytes()
    completed = {}

    async def edit(folder, environment):
        assert (folder / "data.txt").read_bytes() == initial_text
        if change in {"new-script", "empty-canonical"}:
            receipt = await FileWriteTool().execute_with_runtime(
                {"path": "run.sh", "mode": "create", "content": script_contents.decode()},
                ToolRuntime(environment, None, None),
            )
            assert not receipt.startswith("Error")
            assert (await environment.exec_cmd("chmod 755 run.sh")).returncode == 0
            tested = await environment.exec_cmd("./run.sh")
            assert tested.returncode == 0 and tested.stdout == "42\n"
            completed["expected"] = script_contents
        elif change in {"local-context", "overlap"}:
            updated = text.replace(b"line 3\r\n", b"candidate 3\r\n")
            if change == "overlap":
                updated = updated.replace(b"\r\n", b"\n")
            (folder / "data.txt").write_bytes(updated)
            completed["expected"] = updated.replace(b"line 55\r\n", b"user 55\r\n")
        elif change == "binary":
            (folder / "data.bin").write_bytes(b"candidate\0\xfe\n")
            (folder / "data.bin").chmod(0o755)
            (folder / "alias").symlink_to("data.bin")
        elif change == "filter":
            (folder / "data.filtered").write_bytes(b"candidate\n")
        else:
            (folder / "data.utf16").write_bytes("candidate\n".encode("utf-16"))

    async def before_adopt():
        if change in {"local-context", "overlap"}:
            (repo / "data.txt").write_bytes(text.replace(b"line 55\r\n", b"user 55\r\n"))

    if backend == "workflow":
        async def flow(ctx, inputs):
            async def candidate(child, arguments):
                environment = LocalEnvironment(child.workspace_root)
                try:
                    await edit(Path(child.workspace_root), environment)
                finally:
                    await environment.cleanup()
                return "tested"
            result = await ctx.candidate_workflow(candidate, {}, label="physical-bytes")
            completed["canonical"] = result.diff
            completed["physical"] = result.delivery_diff
            await before_adopt()
            if change == "overlap":
                before = (repo / "data.txt").read_bytes()
                with pytest.raises(RuntimeError, match="patch does not apply"):
                    await ctx.adopt_candidate(result)
                assert (repo / "data.txt").read_bytes() == before
                return "conflict preserved"
            await ctx.adopt_candidate(result)
            return "adopted"
        result = await OpenCollab(repo).workflow(flow, trace=False)
        assert result.ok, result.reason
    else:
        pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
        coder = await pool.acquire("coder")
        try:
            await edit(Path(coder.workspace), coder)
            scheduler = _adoption_scheduler(pool, coder)
            await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
            completed["canonical"] = scheduler._completed_coder_diffs[7].patch
            completed["physical"] = scheduler._completed_coder_diffs[7].delivery_diff
            await before_adopt()
            if change == "overlap":
                before = (repo / "data.txt").read_bytes()
                with pytest.raises(RuntimeError, match="full candidate retained"):
                    await scheduler.adopt_agent(0, 7)
                assert (repo / "data.txt").read_bytes() == before
                await coder.get_diff()
            else:
                await scheduler.adopt_agent(0, 7)
                await scheduler.adopt_agent(0, 7)
                if change in {"new-script", "empty-canonical"}:
                    tester = await pool.acquire("tester")
                    tested = await tester.exec_cmd("./run.sh")
                    assert tested.returncode == 0 and tested.stdout == "42\n"
        finally:
            await pool.release()
    if change in {"new-script", "empty-canonical"}:
        assert (repo / "run.sh").read_bytes() == completed["expected"]
        environment = LocalEnvironment(str(repo))
        try:
            tested = await environment.exec_cmd("./run.sh")
            assert tested.returncode == 0 and tested.stdout == "42\n"
        finally:
            await environment.cleanup()
        if change == "empty-canonical":
            assert completed["canonical"] == ""
            assert completed["physical"]
    elif change == "local-context":
        assert (repo / "data.txt").read_bytes() == completed["expected"]
    elif change == "binary":
        assert (repo / "data.bin").read_bytes() == b"candidate\0\xfe\n"
        assert (repo / "data.bin").stat().st_mode & 0o111 == 0o111
        assert (repo / "alias").readlink() == Path("data.bin")
    elif change == "filter":
        assert (repo / "data.filtered").read_bytes() == b"candidate\n"
    elif change == "encoding":
        assert (repo / "data.utf16").read_bytes() == "candidate\n".encode("utf-16")
    assert (repo / ".git/index").read_bytes() == original_index
    assert not list((repo / ".git").glob("opencollab-candidate-*"))
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_linked_team_tester_repeats_the_adopted_lf_script(tmp_path):
    original = _repository(tmp_path)
    (original / ".gitattributes").write_text("*.sh text eol=crlf\n")
    _git(original, "add", ".gitattributes")
    _git(original, "commit", "-m", "Prepare linked worktree attributes")
    source = tmp_path / "linked"
    _git(original, "worktree", "add", "--detach", str(source), "HEAD")
    metadata = Path(_git(source, "rev-parse", "--absolute-git-dir").removesuffix("\n"))
    initial_index = (metadata / "index").read_bytes()
    listing = _git(original, "worktree", "list", "--porcelain")
    pool = WorktreePool(str(source), use_worktrees=True, enable_adoption=True)
    coder = await pool.acquire("coder")
    try:
        receipt = await FileWriteTool().execute_with_runtime(
            {"path": "run.sh", "mode": "create", "content": '#!/bin/sh\nprintf "42\\n"\n'},
            ToolRuntime(coder, None, None),
        )
        assert not receipt.startswith("Error")
        assert (await coder.exec_cmd("chmod 755 run.sh && ./run.sh")).stdout == "42\n"
        scheduler = _adoption_scheduler(pool, coder)
        await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
        await scheduler.adopt_agent(0, 7)
        tester = await pool.acquire("tester")
        tested = await tester.exec_cmd("./run.sh")
        assert tested.returncode == 0 and tested.stdout == "42\n"
        assert Path(tester.workspace, "run.sh").read_bytes() == (source / "run.sh").read_bytes()
    finally:
        await pool.release()
    assert _git(original, "worktree", "list", "--porcelain") == listing
    assert (metadata / "index").read_bytes() == initial_index
    assert not list(metadata.glob("opencollab-candidate-*"))


@pytest.mark.parametrize("path", ["../escape.txt", ".git/escape", "link/escape.txt"])
async def test_physical_delivery_retains_git_path_rejection_and_atomicity(tmp_path, path):
    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
    from opencollab.application.workflow_candidates import CandidateRun

    source = _repository(tmp_path)
    (source / ".gitattributes").write_text("* text eol=crlf\n")
    (source / "link").symlink_to(tmp_path, target_is_directory=True)
    initial_index = (source / ".git/index").read_bytes()

    def addition(name):
        return (f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
                f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+escaped\n")

    patch = addition("first.txt") + addition(path)
    candidate = CandidateRun("invalid", "", patch, (), (), delivery_diff=patch,
                             delivery_paths=("first.txt", path), delivery_source_diff=patch)
    environment = LocalEnvironment(str(source))
    try:
        with pytest.raises(RuntimeError, match="invalid path|outside repository|beyond a symbolic link"):
            await EnvCandidateWorkspace(environment).adopt_run(candidate)
    finally:
        await environment.cleanup()
    assert not (source / "first.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not (source / ".git/escape").exists()
    assert (source / ".git/index").read_bytes() == initial_index
    assert not list((source / ".git").glob("opencollab-candidate-*"))


@pytest.mark.parametrize("backend", ["workflow", "team"])
async def test_adoption_uses_conversion_attributes_saved_after_capture(tmp_path, backend):
    repo = _repository(tmp_path)
    content = b'#!/bin/sh\nprintf "42\\n"\n'
    if backend == "workflow":
        async def flow(ctx, inputs):
            async def edit(child, arguments):
                script = Path(child.workspace_root, "run.sh")
                script.write_bytes(content)
                script.chmod(0o755)
                return "tested"
            candidate = await ctx.candidate_workflow(edit, {}, label="late-attributes")
            assert candidate.delivery_paths == ()
            (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
            await ctx.adopt_candidate(candidate)
        result = await OpenCollab(repo).workflow(flow, trace=False)
        assert result.ok, result.reason
    else:
        pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
        coder = await pool.acquire("coder")
        try:
            script = Path(coder.workspace, "run.sh")
            script.write_bytes(content)
            script.chmod(0o755)
            scheduler = _adoption_scheduler(pool, coder)
            await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
            assert scheduler._completed_coder_diffs[7].delivery_paths == ()
            (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
            await scheduler.adopt_agent(0, 7)
        finally:
            await pool.release()
    assert (repo / "run.sh").read_bytes() == content
    assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"


@pytest.mark.parametrize("converted", [False, True])
async def test_changed_public_candidate_diff_adopts_the_current_canonical_patch(tmp_path, converted):
    repo = _repository(tmp_path)
    if converted:
        (repo / ".gitattributes").write_text("*.py text eol=crlf\n")
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-m", "Prepare public patch replacement")

    async def flow(ctx, inputs):
        async def edit(child, arguments):
            Path(child.workspace_root, "source.py").write_text("value = 42\nprint(value)\n")
        candidate = await ctx.candidate_workflow(edit, {}, label="binding")
        with pytest.raises(ValueError, match="patch must be non-empty"):
            await ctx.adopt_candidate(replace(candidate, diff=""))
        assert (repo / "source.py").read_text() == "value = 1\n"
        alternate = candidate.diff.replace("value = 42", "value = 43")
        selected = replace(candidate, diff=alternate)
        assert selected.effective_diff == alternate
        await ctx.adopt_candidate(selected)

    result = await OpenCollab(repo).workflow(flow, trace=False)
    assert result.ok, result.reason
    expected = b"value = 43\nprint(value)\n"
    assert (repo / "source.py").read_bytes() == (expected.replace(b"\n", b"\r\n") if converted else expected)
    tested = subprocess.run([sys.executable, str(repo / "source.py")], check=True, capture_output=True)
    assert tested.stdout == b"43\n"


async def test_duo_selects_and_adopts_a_physical_only_change(tmp_path):
    from opencollab.builtin_workflows.duo._candidate_records import _adopt
    from opencollab.builtin_workflows.duo._selection import _mechanical_choice

    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
    before = b'#!/bin/sh\r\nprintf "42\\n"\r\n'
    (repo / "run.sh").write_bytes(before)
    (repo / "run.sh").chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Prepare line ending only candidate")

    async def flow(ctx, inputs):
        async def edit(child, arguments):
            Path(child.workspace_root, "run.sh").write_bytes(before.replace(b"\r\n", b"\n"))
        candidate = await ctx.candidate_workflow(edit, {}, label="physical-only")
        assert candidate.diff == "" and candidate.delivery_diff
        empty = replace(candidate, label="empty", delivery_diff="")
        assert _mechanical_choice(candidate, empty) == ("A", "only-a-nonempty", False)
        assert _mechanical_choice(candidate, replace(candidate, delivery_diff="another change"))[2]
        assert await _adopt(ctx, winner="A", candidates={"A": candidate, "B": empty}, preserve_paths=[]) == ("A", ["A"])

    result = await OpenCollab(repo).workflow(flow, trace=False)
    assert result.ok, result.reason
    assert (repo / "run.sh").read_bytes() == before.replace(b"\r\n", b"\n")
    assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"


async def test_team_physical_capture_keeps_strict_replace_object_policy(tmp_path):
    repo = _repository(tmp_path)
    pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
    coder = await pool.acquire("coder")
    original = _git(repo, "rev-parse", "HEAD:source.py").strip()
    current = None
    try:
        Path(coder.workspace, "source.py").write_text("value = 2\n")
        current = _git(Path(coder.workspace), "hash-object", "-w", "source.py").strip()
        _git(repo, "replace", current, original)
        scheduler = _adoption_scheduler(pool, coder)
        await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
        completed = scheduler._completed_coder_diffs[7]
        assert "+value = 2" in completed.patch
        assert "+value = 2" in completed.delivery_diff
        await scheduler.adopt_agent(0, 7)
        assert (repo / "source.py").read_text() == "value = 2\n"
    finally:
        if current is not None:
            _git(repo, "replace", "-d", current)
        await pool.release()


async def test_physical_attribute_overrides_keep_literal_quoted_paths(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "Prepare quoted and newline script name")
    name = 'dir [x]*/name "a space\n.sh'
    content = b'#!/bin/sh\nprintf "42\\n"\n'

    async def flow(ctx, inputs):
        async def edit(child, arguments):
            script = Path(child.workspace_root, name)
            script.parent.mkdir()
            script.write_bytes(content)
            script.chmod(0o755)
        candidate = await ctx.candidate_workflow(edit, {}, label="literal-attributes")
        await ctx.adopt_candidate(candidate)

    result = await OpenCollab(repo).workflow(flow, trace=False)
    assert result.ok, result.reason
    assert (repo / name).read_bytes() == content
    assert subprocess.run([str(repo / name)], check=True, capture_output=True).stdout == b"42\n"


@pytest.mark.parametrize("initial", [False, True])
async def test_team_physical_capture_keeps_info_excluded_files(tmp_path, initial):
    repo = _repository(tmp_path)
    (repo / ".git/info/exclude").write_text("settings.local\n")
    if initial:
        (repo / "settings.local").write_text("source configuration\n")
    pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
    coder = await pool.acquire("coder")
    try:
        path = Path(coder.workspace, "settings.local")
        if initial:
            assert path.read_text() == "source configuration\n"
        path.write_text("candidate configuration\n")
        scheduler = _adoption_scheduler(pool, coder)
        await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
        completed = scheduler._completed_coder_diffs[7]
        assert "+candidate configuration" in completed.patch
        assert "+candidate configuration" in completed.delivery_diff
        await scheduler.adopt_agent(0, 7)
    finally:
        await pool.release()
    assert (repo / "settings.local").read_text() == "candidate configuration\n"


async def test_wrapped_lease_keeps_custom_export_failure_and_candidate(tmp_path):
    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace

    repo = _repository(tmp_path)
    observed = []
    leases = []

    class Wrapper:
        def __init__(self, lease):
            self.lease = lease

        def __getattr__(self, name):
            return getattr(self.lease, name)

        async def diff(self):
            observed.append("custom export")
            raise RuntimeError("custom exporter failed")

    class Workspace(EnvCandidateWorkspace):
        async def acquire(self, label):
            lease = Wrapper(await super().acquire(label))
            leases.append(lease)
            return lease

    environment = LocalEnvironment(str(repo))
    workspace = Workspace(environment)

    async def flow(ctx, inputs):
        async def edit(child, arguments):
            Path(child.workspace_root, "source.py").write_text("value = 2\n")
        await ctx.candidate_workflow(edit, {}, label="wrapped")

    try:
        result = await OpenCollab(repo).workflow(flow, candidate_workspace=workspace, trace=False)
        assert not result.ok and "diff capture failed" in result.reason
        assert observed == ["custom export"]
        assert Path(leases[0].candidate_workspace, "source.py").read_text() == "value = 2\n"
        assert (repo / "source.py").read_text() == "value = 1\n"
    finally:
        for lease in leases:
            await lease.cleanup()
        await environment.cleanup()


@pytest.mark.parametrize("converted", [False, True])
async def test_strict_capture_keeps_scoped_trust_for_an_existing_foreign_repository(tmp_path, monkeypatch, converted):
    repo = _repository(tmp_path)
    if converted:
        (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-m", "Prepare foreign ownership attributes")
    trusted = tmp_path / "trusted-config"
    trusted.write_text("[safe]\n\tdirectory = *\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(trusted))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    control = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if control.returncode == 0:
        pytest.skip("Git does not emulate foreign ownership in this environment")
    assert control.returncode == 128 and b"dubious ownership" in control.stderr
    original_config = (repo / ".git/config").read_bytes()
    original_index = (repo / ".git/index").read_bytes()
    pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
    coder = await pool.acquire("coder")
    try:
        script = Path(coder.workspace, "run.sh")
        script.write_bytes(b'#!/bin/sh\nprintf "42\\n"\n')
        script.chmod(0o755)
        scheduler = _adoption_scheduler(pool, coder)
        await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
        await scheduler.adopt_agent(0, 7)
        assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"
    finally:
        await pool.release()
    assert (repo / ".git/config").read_bytes() == original_config
    assert (repo / ".git/index").read_bytes() == original_index
    assert trusted.read_text() == "[safe]\n\tdirectory = *\n"


async def test_both_candidate_patches_use_the_same_completed_physical_snapshot(tmp_path, monkeypatch):
    from opencollab.adapters import _candidate_physical as physical

    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "Prepare single physical snapshot")
    original = physical._normalize_tree
    late_writes = []

    async def normalize(environment, workspace, tree, **kwargs):
        script = Path(workspace, "run.sh")
        if script.exists() and Path(workspace) != repo:
            script.write_bytes(b'#!/bin/sh\nprintf "99\\n"\n')
            late_writes.append(workspace)
        return await original(environment, workspace, tree, **kwargs)

    monkeypatch.setattr(physical, "_normalize_tree", normalize)

    async def flow(ctx, inputs):
        async def edit(child, arguments):
            script = Path(child.workspace_root, "run.sh")
            script.write_bytes(b'#!/bin/sh\nprintf "42\\n"\n')
            script.chmod(0o755)
            assert subprocess.run([str(script)], check=True, capture_output=True).stdout == b"42\n"
        candidate = await ctx.candidate_workflow(edit, {}, label="single-snapshot")
        assert "42" in candidate.diff and "99" not in candidate.diff
        assert "42" in candidate.delivery_diff and "99" not in candidate.delivery_diff
        await ctx.adopt_candidate(candidate)

    result = await OpenCollab(repo).workflow(flow, trace=False)
    assert result.ok, result.reason
    assert len(late_writes) == 1
    assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"


async def test_converting_candidate_uses_one_owned_metadata_directory_per_stage(tmp_path, monkeypatch):
    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
    from opencollab.application.workflow_candidates import CandidateRun

    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
    initial = b'#!/bin/sh\nprintf "source baseline\\n"\n'
    (repo / "existing.sh").write_bytes(initial)
    (repo / "existing.sh").chmod(0o755)
    _git(repo, "add", ".gitattributes", "existing.sh")
    _git(repo, "commit", "-m", "Prepare private metadata lifecycle")
    initialized = []
    original_exec = LocalEnvironment.exec_cmd

    async def execute(environment, command, *args, **kwargs):
        if " init --bare " in command:
            initialized.append(command)
        return await original_exec(environment, command, *args, **kwargs)

    monkeypatch.setattr(LocalEnvironment, "exec_cmd", execute)
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    lease = await workspace.acquire("metadata-lifetime")
    try:
        assert len(initialized) == 1
        assert Path(lease.environment.workspace, "existing.sh").read_bytes() == initial
        tested = await lease.environment.exec_cmd("./existing.sh")
        assert tested.returncode == 0 and tested.stdout == "source baseline\n"
        script = Path(lease.environment.workspace, "run.sh")
        script.write_bytes(b'#!/bin/sh\nprintf "42\\n"\n')
        script.chmod(0o755)
        canonical = await lease.capture_changes()
        assert len(initialized) == 2
        candidate = CandidateRun("metadata", "tested", canonical, (), (), delivery_diff=lease.delivery_diff,
                                 delivery_paths=lease.delivery_paths, delivery_source_diff=canonical)
        await workspace.adopt_run(candidate)
        assert len(initialized) == 3
        assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"
    finally:
        await lease.cleanup()
        await environment.cleanup()
    assert not list((repo / ".git").glob("opencollab-candidate-*"))


@pytest.mark.parametrize("scope", ["local", "global"])
@pytest.mark.parametrize("backend", ["workflow", "team"])
async def test_private_git_context_does_not_activate_conditional_configuration(tmp_path, monkeypatch, scope, backend):
    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
    from opencollab.application.workflow_candidates import CandidateRun

    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.sh text eol=crlf filter=conditional\n")
    (repo / "existing.sh").write_bytes(b'#!/bin/sh\nprintf "existing\\n"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Prepare conditional private context")
    markers = [tmp_path / name for name in ("monitor-ran", "hook-ran", "filter-ran")]
    monitor = tmp_path / "monitor"
    monitor.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(markers[0]))}\nprintf 'token\\0'\n")
    monitor.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(markers[1]))}\n")
    hook.chmod(0o755)
    conditional = tmp_path / "conditional-config"
    conditional.write_text(
        f"[core]\n\tfsmonitor = {monitor}\n\thooksPath = {hooks}\n"
        f'[filter "conditional"]\n\tclean = touch {shlex.quote(str(markers[2]))}; cat\n'
        f"\tsmudge = touch {shlex.quote(str(markers[2]))}; cat\n"
    )
    key = f"includeIf.gitdir:{repo.resolve()}/.git/opencollab-candidate-*/**.path"
    if scope == "local":
        _git(repo, "config", key, str(conditional))
    else:
        global_config = tmp_path / "global-config"
        _git(repo, "config", "--file", str(global_config), key, str(conditional))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    control = subprocess.run(
        ["git", "-C", str(repo), "config", "--includes", "--get", "core.fsmonitor"], capture_output=True,
    )
    assert control.returncode == 1
    source_index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    lease = pool = None
    try:
        if backend == "workflow":
            lease = await workspace.acquire("conditional-config")
            child = lease.environment
        else:
            pool = WorktreePool(str(repo), use_worktrees=True, enable_adoption=True)
            child = await pool.acquire("coder")
        assert not any(path.exists() for path in markers)
        script = Path(child.workspace, "run.sh")
        script.write_bytes(b'#!/bin/sh\nprintf "42\\n"\n')
        script.chmod(0o755)
        if lease is not None:
            canonical = await lease.capture_changes()
            captured = CandidateRun("conditional", "tested", canonical, (), (), delivery_diff=lease.delivery_diff,
                                    delivery_paths=lease.delivery_paths, delivery_source_diff=canonical)
            await workspace.adopt_run(captured)
        else:
            scheduler = _adoption_scheduler(pool, child)
            await scheduler._append_worktree_diff(child, "tested", aid=7, role="coder")
            await scheduler.adopt_agent(0, 7)
        assert not any(path.exists() for path in markers)
        assert subprocess.run([str(repo / "run.sh")], check=True, capture_output=True).stdout == b"42\n"
        assert (repo / ".git/index").read_bytes() == source_index
    finally:
        if lease is not None:
            await lease.cleanup()
        if pool is not None:
            await pool.release()
        await environment.cleanup()


async def test_physical_capture_streams_index_larger_than_process_capture_limit(tmp_path):
    from opencollab.adapters._env_process import PROCESS_OUTPUT_CAPTURE_BYTES
    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
    from opencollab.application.workflow_candidates import CandidateRun

    repo = _repository(tmp_path)
    (repo / ".gitattributes").write_text("*.py text eol=lf\n")
    for number in range(6_500):
        (repo / f"module{number:05d}_{'x' * 100}.py").write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Prepare index above output limit")
    assert len(_git(repo, "ls-files", "--stage", "-z").encode()) > PROCESS_OUTPUT_CAPTURE_BYTES
    source_index = (repo / ".git/index").read_bytes()
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    lease = None
    try:
        truncated = await environment.exec_cmd("git ls-files --stage -z")
        assert truncated.stdout_truncated
        lease = await workspace.acquire("large-index")
        await lease.environment.write_file("source.py", "print(42)\n")
        canonical = await lease.capture_changes()
        captured = CandidateRun("large", "tested", canonical, (), (), delivery_diff=lease.delivery_diff,
                                delivery_paths=lease.delivery_paths, delivery_source_diff=canonical)
        await workspace.adopt_run(captured)
        tested = await environment.exec_cmd(f"{shlex.quote(sys.executable)} -B source.py")
        assert tested.returncode == 0 and tested.stdout == "42\n"
        assert (repo / ".git/index").read_bytes() == source_index
    finally:
        if lease is not None:
            await lease.cleanup()
        await environment.cleanup()
