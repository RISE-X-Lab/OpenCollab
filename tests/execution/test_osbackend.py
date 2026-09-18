"""Direct tests of the OSBackend / OSEnvironment.

Unlike test_os_mechanism.py (a kernel-primitive probe that reimplements the
steps), this imports and drives the ACTUAL classes shipped in osbackend.py, so
a pass is evidence about the backend code — which is what a review requires.

Requires: Linux, root (CAP_SYS_ADMIN), util-linux, overlayfs, and opencollab
installed (`pip install -e OpenCollab-main`). Run on a privileged Linux host:

    docker run --privileged -it -v "$PWD":/srv -w /srv python:3.11 bash
    pip install -e /path/to/OpenCollab-main fastapi uvicorn httpx pydantic
    apt-get update && apt-get install -y util-linux
    pytest test_osbackend.py -v

It skips itself (not fails) when the kernel prerequisites are absent, so it is
safe to collect anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.linux_only,
    pytest.mark.skipif(
        os.geteuid() != 0 or not shutil.which("unshare") or not shutil.which("nsenter"),
        reason="OSBackend needs Linux + root + util-linux (unshare/nsenter)",
    ),
]

from opencollab.adapters.execution.linux import OSEnvironment  # noqa: E402


# Overlay upperdir cannot live on the container's own overlay rootfs ("not
# supported as upperdir"), so the per-sandbox upper/work go on a tmpfs we mount
# here. The base rootfs (a read-only overlay LOWER) and snapshots may live on an
# ordinary dir. The base is built once per module (a one-time copy of the host
# root), then every test creates sandboxes against it.
@pytest.fixture(scope="module")
def image():
    from opencollab.adapters.execution import linux as osbackend
    selected = os.environ.get("EXECSERVER_TEST_IMAGE")
    if selected:
        osbackend.OSBackend()._resolve_base(selected)
        yield selected
        return
    with tempfile.TemporaryDirectory(prefix="oc-direct-tests-") as directory:
        work = Path(directory)
        os_root, bases, snaps = (work / name for name in ("os", "bases", "snap"))
        os_root.mkdir()
        subprocess.run(["mount", "-t", "tmpfs", "-o", "size=1g", "tmpfs", str(os_root)], check=True)
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("EXECSERVER_OS_ROOT", str(os_root))
                patch.setenv("EXECSERVER_OS_BASES", str(bases))
                patch.setattr(osbackend, "SNAPSHOT_ROOT", snaps)
                osbackend.OSBackend().build_base_from_host("testbase", extra_excludes=("/input", "/srv"))
                yield "testbase"
        finally:
            for root in bases.glob("*/rootfs"):
                subprocess.run(["umount", str(root)], check=True)
            subprocess.run(["umount", str(os_root)], check=True)


@pytest.fixture
def backend(image):
    from opencollab.adapters.execution import linux as osbackend
    return osbackend.OSBackend()


async def _run(env: OSEnvironment, cmd: str, timeout: float = 30.0):
    return await env.exec_cmd(cmd, timeout=timeout)


async def test_create_exec_and_pid_isolation(backend, image):
    sb = await backend.create(image)
    try:
        env: OSEnvironment = sb.env
        r = await _run(env, "echo hi; echo e >&2; exit 4")
        assert r.returncode == 4
        assert r.stdout.strip() == "hi"
        assert "e" in r.stderr
        # Check namespace identity directly; a process-count pipeline creates
        # extra processes and cannot prove that exec entered the right namespace.
        r2 = await _run(env, "readlink /proc/self/ns/pid")
        assert r2.returncode == 0
        assert r2.stdout.strip() == os.readlink(f"/proc/{env._sb.init_pid}/ns/pid")
        assert r2.stdout.strip() != os.readlink("/proc/self/ns/pid")
    finally:
        await backend.destroy(sb)


async def test_init_is_not_the_launcher(backend, image):
    sb = await backend.create(image)
    try:
        osb = sb.env._sb
        assert osb.init_pid is not None and osb.launcher_pid is not None
        assert osb.init_pid != osb.launcher_pid            # the bug review flagged
        host_ns = os.readlink("/proc/self/ns/pid")
        init_ns = os.readlink(f"/proc/{osb.init_pid}/ns/pid")
        assert init_ns != host_ns                          # init really is isolated
    finally:
        await backend.destroy(sb)


async def test_persistence_between_execs(backend, image):
    sb = await backend.create(image)
    try:
        await _run(sb.env, "mkdir -p /opt/state && echo v1 > /opt/state/x")
        r = await _run(sb.env, "cat /opt/state/x")
        assert r.stdout.strip() == "v1"
    finally:
        await backend.destroy(sb)


async def test_checkpoint_restore_covers_whole_env_including_deletions(backend, image):
    sb = await backend.create(image)
    try:
        env = sb.env
        await env.write_file("/workspace/keep.txt", "orig\n")
        await _run(env, "echo base > /opt/base.txt")          # outside workspace
        await backend.snapshot(sb, "cp1")

        # mutate: change a file, create files in and out of workspace, and DELETE
        # a file that existed at checkpoint (exercises overlay whiteouts in tar)
        await env.write_file("/workspace/keep.txt", "CHANGED\n")
        await _run(env, "echo junk > /workspace/g.txt; mkdir -p /opt/thing; rm -f /opt/base.txt")

        # restore = destroy + materialize (what the server's restore does)
        await backend.destroy(sb)
        sb = await backend.materialize("cp1")
        env = sb.env
        assert (await env.read_file("/workspace/keep.txt")) == "orig\n"
        assert (await _run(env, "test -f /workspace/g.txt && echo y || echo n")).stdout.strip() == "n"
        assert (await _run(env, "test -e /opt/thing && echo y || echo n")).stdout.strip() == "n"
        # the deleted file is back (snapshot captured state BEFORE deletion)
        assert (await _run(env, "cat /opt/base.txt")).stdout.strip() == "base"
    finally:
        await backend.destroy(sb)
        await backend.discard("cp1")


async def test_fork_is_independent(backend, image):
    sb = await backend.create(image)
    child = None
    try:
        await sb.env.write_file("/workspace/n.txt", "orig\n")
        await backend.snapshot(sb, "cp2")
        child = await backend.materialize("cp2")
        await child.env.write_file("/workspace/n.txt", "branchB\n")
        assert (await sb.env.read_file("/workspace/n.txt")) == "orig\n"
        assert (await child.env.read_file("/workspace/n.txt")) == "branchB\n"
    finally:
        await backend.destroy(sb)
        if child is not None:
            await backend.destroy(child)
        await backend.discard("cp2")


async def test_cancel_reaps_escaped_child_and_proves_quiescence(backend, image):
    sb = await backend.create(image)
    try:
        env = sb.env
        # a command that backgrounds a child which would write a sentinel later
        await _run(env, "(sleep 3; echo leaked > /workspace/sentinel.txt) & echo started")
        # Exec must still enter the sandbox after cleaning up the child.
        r = await _run(env, "readlink /proc/self/ns/pid")
        assert r.returncode == 0
        assert r.stdout.strip() == os.readlink(f"/proc/{env._sb.init_pid}/ns/pid")
        assert r.stdout.strip() != os.readlink("/proc/self/ns/pid")
        # wait past the child's deadline; the sentinel must never appear
        await _run(env, "sleep 4")
        got = await _run(env, "test -f /workspace/sentinel.txt && echo LEAK || echo clean")
        assert got.stdout.strip() == "clean"
    finally:
        await backend.destroy(sb)


async def test_remove_file_ownership_semantics_preserved(backend, image):
    sb = await backend.create(image)
    try:
        env = sb.env
        tmp = await env.write_temp_file("x", prefix="oc-", suffix=".tmp")
        assert tmp.startswith("/workspace/")
        await env.remove_file(tmp)                         # owned -> ok
        await env.write_file("/workspace/b.txt", "y\n")
        with pytest.raises(OSError):
            await env.remove_file("/workspace/b.txt")      # unowned -> refused
    finally:
        await backend.destroy(sb)


async def test_base_is_immutable_and_digested(backend, image):
    """Sandbox writes must not touch the shared base rootfs, and the base
    carries a stable content DIGEST."""
    from opencollab.adapters.execution import linux as osbackend
    b = osbackend.OSBackend()
    _base_id, rootfs, digest = b._resolve_base(image)
    assert digest.startswith("sha256:")
    sb = await backend.create(image)
    try:
        await _run(sb.env, "mkdir -p /opt/sbxonly && echo hi > /opt/sbxonly/f")
        await _run(sb.env, "echo mutated > /etc/os-release || true")
    finally:
        await backend.destroy(sb)
    # writes landed in the sandbox upper, NOT in the immutable base
    assert not (rootfs / "opt" / "sbxonly").exists()
    assert b._resolve_base(image)[2] == digest       # digest unchanged


async def test_unknown_image_is_rejected_not_ignored(backend):
    """A base that was never built must raise, not silently fall back to the
    live host root (the old silent-ignore bug)."""
    with pytest.raises(RuntimeError):
        await backend.create("no-such-base-does-not-exist")


async def test_snapshot_preserves_deletion_of_lower_file(backend, image):
    sb = await backend.create(image)
    clone = None
    try:
        before = await _run(sb.env, "test -e /etc/os-release")
        assert before.returncode == 0
        removed = await _run(sb.env, "rm /etc/os-release")
        assert removed.returncode == 0
        await backend.snapshot(sb, "cp-whiteout")
        clone = await backend.materialize("cp-whiteout")
        result = await _run(clone.env, "test ! -e /etc/os-release")
        assert result.returncode == 0
    finally:
        if clone is not None:
            await backend.destroy(clone)
        await backend.destroy(sb)
        await backend.discard("cp-whiteout")
