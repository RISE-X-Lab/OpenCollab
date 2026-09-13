"""Rootful Linux namespace and OverlayFS execution backend."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from opencollab.adapters._env_base import ExecResult, TextFileRange
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import (
    ProcessCleanupError,
    ProcessRegistry,
    run_process,
    timed_out_result,
)

from .backend import Sandbox
from .cgroup import SandboxCgroup

log = logging.getLogger(__name__)

SANDBOX_WORKSPACE = "/workspace"
SNAPSHOT_ROOT = Path(os.environ.get("EXECSERVER_OS_SNAPSHOTS", "/var/lib/execserver/snap"))
HOST_PID_NS = os.readlink("/proc/self/ns/pid") if os.path.exists("/proc/self/ns/pid") else ""


async def _host(*argv: str, timeout: float = 60.0, check: bool = True) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"host {argv[:2]} rc={proc.returncode}: {err.decode(errors='replace')}")
    return proc.returncode or 0, out, err


class OSSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cgroup: SandboxCgroup | None = None
        self.merged = root / "merged"
        self.upper = root / "upper"
        self.work = root / "work"
        self.workspace_host = self.merged / "workspace"
        self.launcher_pid: int | None = None   # the `unshare` process (host pid ns)
        self.init_pid: int | None = None        # its child = PID 1 of the new pid ns
        # base identity: locator re-resolves the SAME base; digest is its
        # verified content version. Set by _new_sandbox.
        self.base_locator: str = "host-live"
        self.base_digest: str = "host-live-unpinned"


class OSEnvironment:
    """EnvironmentPort over an OSSandbox. Exec runs inside the namespaces; file
    ops run host-side on the overlay merged dir (reusing LocalEnvironment's
    semantics) but are presented rooted at /workspace."""

    local_filesystem = False
    process_isolated = True

    def __init__(self, sandbox: OSSandbox) -> None:
        self._sb = sandbox
        self.workspace = SANDBOX_WORKSPACE
        self.host_workspace: str | None = None
        self.source_workspace: str | None = None
        self._revoked = False
        self._processes = ProcessRegistry()
        self._exec_lock = asyncio.Lock()   # v1 serializes execs per sandbox
        self._files = LocalEnvironment(os.fspath(sandbox.workspace_host))

    @property
    def revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        self._revoked = True

    def _ensure(self) -> None:
        if self._revoked:
            raise RuntimeError("OS environment revoked")
        if self._sb.init_pid is None:
            raise RuntimeError("sandbox not started")

    async def setup(self, mount_dir: str | None = None) -> str:
        if mount_dir is not None:
            raise ValueError("mount_dir unsupported by OS environment")
        return self.workspace

    async def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            await self._files.cleanup()

    async def abort(self) -> None:
        self.revoke()
        with contextlib.suppress(Exception):
            await self._processes.abort()
        if self._sb.cgroup is not None:
            await self._sb.cgroup.close()
        await self.cleanup()

    # -- exec --------------------------------------------------------------

    def _nsenter(self, cmd: str) -> list[str]:
        # Stop background writers before inherited output pipes hold the host
        # collector open. The host-side reap still verifies quiescence afterward.
        inner = (
            f"cd {SANDBOX_WORKSPACE} || exit; (\n{cmd}\n); oc_status=$?; "
            'for p in /proc/[0-9]*; do pid=${p#/proc/}; '
            'case "$pid" in 1|"$$") continue;; esac; '
            'kill -9 "$pid" 2>/dev/null; done; exit "$oc_status"'
        )
        return self._sb.cgroup.wrap(["nsenter", "-t", str(self._sb.init_pid), "-m", "-p", "-n", "--",
                                    "/bin/sh", "-c", inner])

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure()
        async with self._exec_lock:
            self._ensure()
            argv = self._nsenter(cmd)
            try:
                result = await run_process(
                    argv, shell=False, timeout=timeout, registry=self._processes)
                result = result.to_exec_result()
            except asyncio.TimeoutError as exc:
                result = timed_out_result(exc, -1, timeout)
            except ProcessCleanupError as exc:
                if not self._sb.cgroup.oom_kills():
                    self.revoke()
                    await self._sb.cgroup.close()
                    raise
                # The group OOM kill can leave zombies visible to killpg even
                # after all live members exited. The kernel cgroup state below
                # supplies the quiescence evidence in this case.
                result = ExecResult(-1, "", str(exc))
            except asyncio.CancelledError:
                if not await self._reap_sandbox_processes():
                    self.revoke()   # could not prove quiescence -> sandbox is unsafe
                    await self._sb.cgroup.close()
                raise
            if self._sb.cgroup.oom_kills():
                self.revoke()
                # An OOM group kill may still be completing. Wait for the whole
                # group before returning an actionable failed command result.
                deadline = time.monotonic() + 5.0
                while self._sb.cgroup.populated():
                    if time.monotonic() >= deadline:
                        await self._sb.cgroup.close()
                        break
                    await asyncio.sleep(0.01)
                result.stderr += "\nSandbox memory limit exceeded (OOM); sandbox revoked."
                if result.returncode == 0:
                    result.returncode = -1
                return result
            # after ANY outcome, guarantee no command process outlived the call
            if not await self._reap_sandbox_processes():
                # the process tree did not go quiet -> revoke so the server tears
                # the sandbox down rather than trust a dirty environment.
                self.revoke()
                await self._sb.cgroup.close()
                raise RuntimeError("sandbox did not quiesce after exec; revoked")
            return result

    async def _reap_sandbox_processes(self, deadline: float = 5.0) -> bool:
        """Kill every process in the sandbox PID namespace except its init, then
        WAIT until only init remains. Returns True iff quiescence is proven.

        The sweep excludes its own shell ($$). It is not enough to send SIGKILL:
        we poll until the process set is back to {init} (plus the transient
        counting shell) or the deadline passes, in which case the caller revokes
        the sandbox."""
        if self._sb.init_pid is None or not self._sb.cgroup.populated():
            self.revoke()
            return True
        pid = str(self._sb.init_pid)
        sweep = (
            'self=$$; for p in /proc/[0-9]*; do pid=${p#/proc/}; '
            'case "$pid" in 1|"$self") continue;; esac; '
            'kill -9 "$pid" 2>/dev/null; done'
        )
        # count only RUNNING processes: a zombie (state Z) is already dead, not a
        # process that outlived the command, so it does not count against
        # quiescence. init reaps zombies within a moment anyway.
        count_running = (
            'n=0; for pp in /proc/[0-9]*; do p=${pp#/proc/}; '
            'st=$(cut -d" " -f3 "$pp/stat" 2>/dev/null); '
            '[ "$st" = "Z" ] && continue; n=$((n+1)); done; echo $n'
        )
        end = time.monotonic() + deadline
        while True:
            with contextlib.suppress(Exception):
                await _host("nsenter", "-t", pid, "-m", "-p", "--",
                            "/bin/sh", "-c", sweep, check=False)
            rc, out, _ = await _host(
                "nsenter", "-t", pid, "-m", "-p", "--", "/bin/sh", "-c",
                count_running, check=False)
            try:
                n = int(out.decode().strip())
            except ValueError:
                n = 99
            if n <= 2:   # init (1) + the transient counting shell
                return True
            if time.monotonic() > end:
                return False
            await asyncio.sleep(0.1)

    # -- files (host-side on the overlay, presented at /workspace) ---------

    def _rel(self, path: str) -> str:
        if path == SANDBOX_WORKSPACE:
            return "."
        prefix = SANDBOX_WORKSPACE + "/"
        if path.startswith(prefix):
            return path[len(prefix):]
        if path.startswith("/"):
            raise PermissionError(f"path escapes sandbox workspace: {path}")
        return path

    async def read_file(self, path: str) -> str:
        self._ensure()
        return await self._files.read_file(self._rel(path))

    async def read_text_range(self, path: str, *, offset: int, limit: int,
                              max_chars: int) -> TextFileRange:
        self._ensure()
        return await self._files.read_text_range(
            self._rel(path), offset=offset, limit=limit, max_chars=max_chars)

    async def write_file(self, path: str, content: str) -> None:
        self._ensure()
        await self._files.write_file(self._rel(path), content)

    async def write_temp_file(self, content: str, *, prefix: str, suffix: str = ".tmp") -> str:
        self._ensure()
        host_path = await self._files.write_temp_file(content, prefix=prefix, suffix=suffix)
        return f"{SANDBOX_WORKSPACE}/{os.path.basename(host_path)}"

    async def remove_file(self, path: str) -> None:
        self._ensure()
        await self._files.remove_file(self._rel(path))

    @property
    def _temporary_files(self) -> set[str]:
        """Expose LocalEnvironment ownership metadata to snapshot orchestration."""
        return self._files._temporary_files  # noqa: SLF001

    @_temporary_files.setter
    def _temporary_files(self, paths: set[str]) -> None:
        self._files._temporary_files = set(paths)  # noqa: SLF001


class OSBackend:
    """SandboxBackend from Linux namespaces + OverlayFS + per-sandbox cgroup v2. No Docker."""

    def __init__(self, root_dir: str | None = None) -> None:
        self._base = Path(root_dir or os.environ.get("EXECSERVER_OS_ROOT", "/var/lib/execserver/os"))
        self._base.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        # Prepared, immutable base rootfs images: <bases>/<name>/rootfs + DIGEST.
        # create(image) selects one BY NAME; no silent fall-through to live /.
        self._bases = Path(os.environ.get("EXECSERVER_OS_BASES", "/var/lib/execserver/bases"))
        self._bases.mkdir(parents=True, exist_ok=True)

    def _snap_tar(self, ref: str) -> Path:
        return SNAPSHOT_ROOT / f"{ref}.tar"

    def _snap_base(self, ref: str) -> Path:
        return SNAPSHOT_ROOT / f"{ref}.base"

    @staticmethod
    def _valid_name(name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name == "host-live":
            raise ValueError("base name must be a single safe path component (not host-live)")

    @staticmethod
    def _stored_digest(digest_file: Path) -> str:
        try:
            digest = digest_file.read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"missing base DIGEST: {digest_file}") from exc
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError(f"malformed base DIGEST: {digest_file}")
        return digest

    @staticmethod
    def _protect_base(rootfs: Path) -> None:
        # Preserve image permission bits; chmod would change both its identity
        # and the permissions later seen by sandbox copy-up operations.
        mounted = subprocess.run(["mountpoint", "-q", str(rootfs)]).returncode == 0
        if not mounted:
            subprocess.run(["mount", "--bind", str(rootfs), str(rootfs)], check=True,
                           capture_output=True)
        try:
            subprocess.run(["mount", "-o", "remount,bind,ro", str(rootfs)], check=True,
                           capture_output=True)
        except BaseException:
            if not mounted:
                subprocess.run(["umount", str(rootfs)], capture_output=True)
            raise

    def _verify_base(self, rootfs: Path, digest_file: Path) -> str:
        stored = self._stored_digest(digest_file)
        if not rootfs.is_dir():
            raise FileNotFoundError(f"missing base rootfs: {rootfs}")
        # Protect before reading, rather than hashing a writable tree and then
        # opening a mutation window before applying read-only protection.
        self._protect_base(rootfs)
        actual = self._compute_digest(rootfs)
        if actual != stored:
            raise RuntimeError(f"base digest mismatch: recorded {stored}, actual {actual}")
        return actual

    def _resolve_base(self, image: str) -> tuple[str, Path, str]:
        if image == "host-live":
            log.warning("host-live is an unpinned development base; immutable guarantees do not apply")
            return image, Path("/"), "host-live-unpinned"
        if os.path.isabs(image):
            root = Path(image).resolve(strict=True)
            if root == Path("/"):
                raise ValueError("use host-live explicitly for the live root")
        else:
            self._valid_name(image)
            candidate = self._bases / image / "rootfs"
            if not candidate.is_dir():
                raise RuntimeError(f"unknown base image {image!r}; build it before create")
            root = candidate.resolve(strict=True)
        return image, root, self._verify_base(root, root.parent / "DIGEST")

    def build_base_from_host(self, name: str, extra_excludes: tuple[str, ...] = (),
                             *, source_root: Path = Path("/")) -> str:
        """Copy a quiescent source, protect it read-only, then publish its digest.

        Privileged administrators can deliberately unmount or replace a base;
        this protects normal writes, not attacks by the runtime administrator.
        Existing names are verified and reused, never silently overwritten.
        """
        self._valid_name(name)
        source = Path(source_root).resolve(strict=True)
        if not source.is_dir():
            raise NotADirectoryError(source)
        dest = self._bases / name
        if dest.exists():
            return self._verify_base(dest / "rootfs", dest / "DIGEST")
        stage = Path(tempfile.mkdtemp(prefix=".building-", dir=self._bases))
        rootfs = stage / "rootfs"
        rootfs.mkdir()
        published = False
        try:
            runtime_dirs = ("proc", "sys", "dev", "run", "tmp", "mnt", "media")
            excludes = ([f"./{name}" for name in runtime_dirs] + ["./var/lib/execserver"]
                        if source == Path("/") else [])
            for excluded in (self._bases.resolve(), self._base.resolve(),
                             SNAPSHOT_ROOT.resolve(), *map(Path, extra_excludes)):
                try:
                    rel = excluded.resolve().relative_to(source)
                except ValueError:
                    continue
                if rel == Path("."):
                    raise ValueError("cannot exclude the entire source root")
                excludes.append("./" + rel.as_posix())
            producer = ["tar", "--one-file-system", "--numeric-owner", "--xattrs",
                        "--xattrs-include=*", *[f"--exclude={p}" for p in excludes],
                        "-C", str(source), "-cf", "-", "."]
            consumer = ["tar", "--numeric-owner", "--xattrs", "--xattrs-include=*",
                        "-C", str(rootfs), "-xf", "-"]
            # Quote argv and use pipefail so a failed producer cannot publish
            # a digest for an incomplete but successfully extracted archive.
            pipeline = shlex.join(producer) + " | " + shlex.join(consumer)
            result = subprocess.run(["bash", "-o", "pipefail", "-c", pipeline], capture_output=True)
            if result.returncode:
                raise RuntimeError(f"base build failed: {result.stderr.decode(errors='replace')[:800]}")
            if source == Path("/"):
                # Exclude the virtual mountpoints themselves, whose directory
                # metadata changes during reads; recreate empty runtime paths.
                for directory in runtime_dirs:
                    (rootfs / directory).mkdir(exist_ok=True)
                (rootfs / "tmp").chmod(0o1777)
            self._protect_base(rootfs)
            digest = self._compute_digest(rootfs)
            (stage / "DIGEST").write_text(digest + "\n")
            # A directory becomes selectable only after extraction, protection,
            # and hashing succeed. rename refuses an existing nonempty name.
            stage.rename(dest)
            published = True
            return digest
        finally:
            if not published:
                if subprocess.run(["mountpoint", "-q", str(rootfs)]).returncode == 0:
                    subprocess.run(["umount", str(rootfs)], check=True, capture_output=True)
                shutil.rmtree(stage)

    @staticmethod
    def _compute_digest(rootfs: Path) -> str:
        if not rootfs.is_dir():
            raise FileNotFoundError(f"cannot digest missing rootfs {rootfs}")
        # Ignore access/change times (reading changes atime); retain modes,
        # mtimes, ownership, symlinks, hardlinks, devices, and extended attributes.
        argv = ["tar", "--sort=name", "--format=pax", "--numeric-owner", "--xattrs",
                "--xattrs-include=*",
                "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime",
                "-C", str(rootfs), "-cf", "-", "."]
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors)
            digest = hashlib.sha256()
            try:
                while block := proc.stdout.read(1024 * 1024):
                    digest.update(block)
                code = proc.wait()
            finally:
                proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if code:
                errors.seek(0)
                raise RuntimeError(f"digest computation failed: {errors.read(800).decode(errors='replace')}")
        return "sha256:" + digest.hexdigest()

    # -- SandboxBackend protocol ------------------------------------------

    async def create(self, image: str) -> Sandbox:
        base_id, rootfs, digest = self._resolve_base(image)
        sb = await self._new_sandbox(lower=rootfs, base_id=base_id, base_digest=digest, seed_tar=None)
        return Sandbox(OSEnvironment(sb), os.fspath(sb.root))

    async def destroy(self, sb_wrapper: Sandbox) -> None:
        env: OSEnvironment = sb_wrapper.env
        env.revoke()
        if env._sb.cgroup is not None:
            await env._sb.cgroup.close()
        # Release the file adapter's directory fd before a non-lazy unmount.
        await env.cleanup()
        await self._teardown(env._sb)

    async def snapshot(self, sb_wrapper: Sandbox, ref: str) -> None:
        env = sb_wrapper.env
        locator, _, digest = self._resolve_base(env._sb.base_locator)
        if digest != env._sb.base_digest:
            raise RuntimeError("base version changed since sandbox creation")
        tar = self._snap_tar(ref)
        await _host("tar", "--xattrs", "--xattrs-include=*", "--numeric-owner",
                    "-C", os.fspath(env._sb.upper), "-cf", os.fspath(tar), ".")
        self._snap_base(ref).write_text(json.dumps({"locator": locator, "digest": digest}))

    async def materialize(self, ref: str) -> Sandbox:
        tar = self._snap_tar(ref)
        if not tar.exists():
            raise FileNotFoundError(f"snapshot {ref} not found")
        try:
            metadata = json.loads(self._snap_base(ref).read_text())
            locator, expected = metadata["locator"], metadata["digest"]
            if not isinstance(locator, str) or not isinstance(expected, str):
                raise ValueError("invalid metadata types")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("snapshot base metadata missing or invalid; refusing fallback") from exc
        locator, lower, digest = self._resolve_base(locator)
        if digest != expected:
            raise RuntimeError("snapshot base version/digest no longer matches")
        sb = await self._new_sandbox(lower=lower, base_id=locator,
                                     base_digest=digest, seed_tar=tar)
        return Sandbox(OSEnvironment(sb), os.fspath(sb.root))

    async def discard(self, ref: str) -> None:
        for pth in (self._snap_tar(ref), self._snap_base(ref)):
            if pth.exists():
                await asyncio.to_thread(pth.unlink)

    def alive(self, sb_wrapper: Sandbox) -> bool:
        env: OSEnvironment = sb_wrapper.env
        pid = env._sb.init_pid
        if pid is None or env.revoked:
            return False
        return Path(f"/proc/{pid}").exists()

    # -- sandbox construction ---------------------------------------------

    async def _new_sandbox(self, *, lower: Path, base_id: str, base_digest: str,
                           seed_tar: Path | None) -> OSSandbox:
        root = Path(tempfile.mkdtemp(prefix="sbx-", dir=self._base))
        sb = OSSandbox(root)
        sb.base_locator = base_id
        sb.base_digest = base_digest
        try:
            sb.cgroup = SandboxCgroup()
            for d in (sb.merged, sb.upper, sb.work):
                d.mkdir(parents=True, exist_ok=True)
            if seed_tar is not None:
                # restore/fork: seed this sandbox's upper from a snapshot tar
                await _host("tar", "--xattrs", "--xattrs-include=*", "--numeric-owner", "-C", os.fspath(sb.upper),
                            "-xf", os.fspath(seed_tar))
            await _host("mount", "-t", "overlay", "overlay",
                        "-o", f"lowerdir={os.fspath(lower)},upperdir={sb.upper},workdir={sb.work}",
                        os.fspath(sb.merged))
            (sb.merged / "workspace").mkdir(exist_ok=True)
            (sb.merged / ".oldroot").mkdir(exist_ok=True)
            await self._launch_init(sb)
            return sb
        except BaseException:
            await self._teardown(sb)
            raise

    async def _launch_init(self, sb: OSSandbox) -> None:
        # Reap independently of SIGCHLD delivery. Coalesced/emulated signals
        # must not leave adopted zombies visible to the host process-group probe.
        # Blocking wait handles children promptly; sleep only when none exist.
        init_code = """import os, time

while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        time.sleep(0.05)
"""
        script = (
            f"set -eu; mount --make-rprivate / ; cd {shlex.quote(str(sb.merged))} ; "
            "pivot_root . .oldroot ; mount -t proc proc /proc ; "
            # The overlay lower does not include the host's /dev mount. Give
            # tools a private device directory, including git's random source.
            "mount -t tmpfs -o nosuid,mode=755 tmpfs /dev ; "
            "mknod -m 666 /dev/null c 1 3 ; mknod -m 666 /dev/zero c 1 5 ; "
            "mknod -m 666 /dev/random c 1 8 ; mknod -m 666 /dev/urandom c 1 9 ; "
            "ln -s /proc/self/fd /dev/fd ; "
            "ln -s /proc/self/fd/0 /dev/stdin ; "
            "ln -s /proc/self/fd/1 /dev/stdout ; "
            "ln -s /proc/self/fd/2 /dev/stderr ; "
            "umount -l /.oldroot ; rmdir /.oldroot ; "
            "exec python3 -c " + shlex.quote(init_code)
        )
        proc = await asyncio.create_subprocess_exec(*sb.cgroup.wrap([
            "unshare", "--mount", "--pid", "--net", "--fork", "--",
            "/bin/sh", "-c", script]))
        sb.launcher_pid = proc.pid
        # The launcher (`unshare`) stays in the HOST pid ns; the REAL init is its
        # forked child (PID 1 of the new ns). Resolve and verify it — using the
        # launcher pid for nsenter -p would land back in the host pid namespace.
        sb.init_pid = await self._resolve_init(proc.pid)
        await self._verify_isolated(sb)

    async def _resolve_init(self, launcher_pid: int, deadline: float = 3.0) -> int:
        children = Path(f"/proc/{launcher_pid}/task/{launcher_pid}/children")
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            with contextlib.suppress(Exception):
                kids = children.read_text().split()
                if kids:
                    return int(kids[0])
            await asyncio.sleep(0.05)
        raise RuntimeError("could not resolve sandbox init pid (child of unshare)")

    async def _verify_isolated(self, sb: OSSandbox) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                ns = os.readlink(f"/proc/{sb.init_pid}/ns/pid")
                if ns == HOST_PID_NS:
                    raise RuntimeError("sandbox init still in host PID namespace")
                comm = Path(f"/proc/{sb.init_pid}/comm").read_text().strip()
                if comm.startswith("python"):
                    rc, out, _ = await _host(
                        "nsenter", "-t", str(sb.init_pid), "-m", "-p", "-n", "--",
                        "readlink", "/proc/self/ns/pid", check=False)
                    if rc == 0 and out.decode().strip() == ns:
                        return
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.02)
        raise RuntimeError("sandbox init failed its namespace/readiness check")

    async def _teardown(self, sb: OSSandbox) -> None:
        if sb.cgroup is not None:
            await sb.cgroup.close()
        else:
            # Construction may fail before a group exists; no process has been
            # launched yet. Do not report cleanup success for a failed kill.
            for pid in (sb.init_pid, sb.launcher_pid):
                if pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        sb.init_pid = sb.launcher_pid = None
        if subprocess.run(["mountpoint", "-q", str(sb.merged)]).returncode == 0:
            await _host("umount", os.fspath(sb.merged))
        if sb.root.exists():
            await asyncio.to_thread(shutil.rmtree, sb.root)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build a verified read-only OS sandbox base")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build-base")
    build_parser.add_argument("name")
    build_parser.add_argument("--source", type=Path, default=Path("/"))
    build_parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    backend = OSBackend()
    digest = backend.build_base_from_host(args.name, tuple(args.exclude), source_root=args.source)
    print(f"base ready: {backend._bases / args.name / 'rootfs'}")
    print(f"DIGEST {digest}")
