from __future__ import annotations

import asyncio
import errno
import os
import resource
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_local import LocalEnvironment
from tests.workflows.test_candidate_adoption_recovery import _detached_files
from tests.workflows.test_workflow_candidate_workspace import _git


class _ArgumentBoundEnvironment(LocalEnvironment):
    async def exec_cmd(self, command, timeout=120.0, **kwargs):
        if len(command.encode()) > 65_536:
            raise OSError(errno.E2BIG, "transport command argument is too long")
        return await super().exec_cmd(command, timeout, **kwargs)


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("file_count,already_open", [(900, 0), (120, 120)], ids=["bulk", "busy-process"])
def test_bulk_candidate_adoption_keeps_recovery_below_process_limits(tmp_path, blocked, file_count, already_open):
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = 256 if soft == resource.RLIM_INFINITY else min(soft, 256)
    if already_open and limit < 256:
        pytest.skip("the busy process case uses a 256 descriptor limit")
    completed = subprocess.run(
        [
            sys.executable, "-m", __name__, str(tmp_path),
            str(int(blocked)), str(file_count), str(already_open), str(limit),
        ],
        cwd=Path(__file__).resolve().parents[2],
        close_fds=True, capture_output=True, text=True, timeout=180,
    )
    if completed.returncode:
        pytest.fail(
            f"candidate child exited with status {completed.returncode}\n{completed.stdout}{completed.stderr}",
            pytrace=False,
        )


async def _exercise_bulk_candidate(tmp_path: Path, blocked: bool, file_count: int, already_open: int) -> None:
    # Long absolute paths expand the recovery script past the temp-file limit
    # even though the repository-relative patch fits under one megabyte.
    repo = tmp_path.joinpath(*("nested-" + "workspace-" * 8,) * 5, "repo")
    (repo / "src").mkdir(parents=True)
    (repo / "settings").mkdir()
    names = [f"{number:04d}_" + "module_" * 10 + ".py" for number in range(file_count)]
    for name in names:
        (repo / "src" / name).write_text("VALUE = 1\n")
    (repo / "settings/value.txt").write_text("original settings\n")
    (repo / ".gitignore").write_text("*.cache\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare a bulk candidate")
    if blocked:
        (repo / "settings/session.cache").write_text("runtime cache\n")
    index = (repo / ".git/index").read_bytes()
    first_module = repo / "src" / names[0]
    original_inode = first_module.stat().st_ino
    environment = _ArgumentBoundEnvironment(str(repo))
    adoption_failure: RuntimeError | None = None

    async def edit(child, inputs):
        folder = Path(child.workspace_root)
        for name in names:
            (folder / "src" / name).write_text("VALUE = 2\n")
        (folder / "settings/value.txt").unlink()
        (folder / "settings").rmdir()
        (folder / "settings").write_text("new settings\n")
        return "updated"

    async def flow(ctx, inputs):
        nonlocal adoption_failure
        candidate = await ctx.candidate_workflow(edit, {}, label="bulk-rewrite")
        assert len(candidate.effective_diff.encode()) < 1_000_000
        with ExitStack() as descriptors:
            for _ in range(already_open):
                descriptors.enter_context(open(os.devnull, "rb"))
            if blocked:
                with pytest.raises(RuntimeError, match="Directory not empty|Is a directory") as failed:
                    await ctx.adopt_candidate(candidate)
                adoption_failure = failed.value
            else:
                await ctx.adopt_candidate(candidate)
        return "finished"

    try:
        result = await OpenCollab(repo, environment=environment).workflow(flow, {}, trace=False)
        assert result.ok, result.reason
        if environment.revoked:
            raise AssertionError("candidate adoption revoked the source environment") from adoption_failure
        expected = "VALUE = 1\n" if blocked else "VALUE = 2\n"
        assert all((repo / "src" / name).read_text() == expected for name in names)
        if blocked:
            assert (repo / "settings/value.txt").read_text() == "original settings\n"
            assert (repo / "settings/session.cache").read_text() == "runtime cache\n"
            originals, _ = _detached_files(repo / ".git")
            assert originals[repo / "settings/value.txt"] == b"original settings\n"
            assert all(originals[repo / "src" / name] == b"VALUE = 1\n" for name in names)
            record, = (
                path for path in (repo / ".git").glob("candidate-recovery-*/original-inodes/*.path")
                if Path(path.read_text()) == first_module
            )
            assert record.with_suffix("").stat().st_ino == original_inode
        else:
            assert (repo / "settings").read_text() == "new settings\n"
            assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").rglob(".candidate-command-*.sh"))
    finally:
        await environment.cleanup()


if __name__ == "__main__":
    directory, blocked, file_count, already_open, limit = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(limit), int(limit)))
    asyncio.run(_exercise_bulk_candidate(Path(directory), bool(int(blocked)), int(file_count), int(already_open)))
