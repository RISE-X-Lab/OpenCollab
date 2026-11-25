from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import resource
import signal
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools import base
from opencollab.adapters.tools._file_lock_worker import _try_file_lock
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime


@pytest.mark.parametrize("use_workers", [False, True], ids=["direct", "worker"])
async def test_native_sibling_writes_share_ancestors_and_exclude_transactions(tmp_path, monkeypatch, use_workers):
    if use_workers:
        monkeypatch.setattr(base, "_local_file_lock_capacity", lambda: 2)
    monkeypatch.setattr(base, "HOST_WRITE_LOCK_TIMEOUT_SECONDS", 0.25)
    directory = tmp_path / "nested"
    directory.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (directory / name).write_text("before\n")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    transaction_paths = ("nested", "other.txt", "unrelated.txt")
    started, finish = asyncio.Event(), asyncio.Event()
    read_file = env.read_file

    async def pause_first_read(path):
        if Path(path).name == "one.txt" and not started.is_set():
            started.set()
            await finish.wait()
        return await read_file(path)

    monkeypatch.setattr(env, "read_file", pause_first_read)

    async def write(name):
        return await FileWriteTool().execute_with_runtime(
            {"path": f"nested/{name}", "mode": "str_replace", "old_str": "before", "new_str": "after"}, runtime,
        )

    first = asyncio.create_task(write("one.txt"))
    try:
        try:
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.wait_for(write("two.txt"), 5)
            assert (directory / "two.txt").read_text() == "after\n"
            assert (directory / "one.txt").read_text() == "before\n"
            with pytest.raises(TimeoutError, match="one.txt"):
                await write("one.txt")
            with pytest.raises(TimeoutError, match="nested$"):
                async with base.host_write_locks(transaction_paths, env):
                    pytest.fail("transaction acquired a directory with an active child edit")
        finally:
            finish.set()
            await first

        assert (directory / "one.txt").read_text() == "after\n"
        async with base.host_write_locks(transaction_paths, env):
            with pytest.raises(TimeoutError, match="nested$"):
                await write("three.txt")
        assert (directory / "three.txt").read_text() == "before\n"
        await write("three.txt")
        assert (directory / "three.txt").read_text() == "after\n"
    finally:
        await env.cleanup()


def test_shared_flock_rejects_inode_unlinked_before_acquisition(tmp_path, monkeypatch):
    path = tmp_path / "directory.lock"
    acquire_exclusive = FileLock(path, timeout=0)
    acquire_exclusive.acquire()
    open_file = os.open

    def open_then_release(*args, **kwargs):
        fd = open_file(*args, **kwargs)
        path.unlink()
        acquire_exclusive.release()
        return fd

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", open_then_release)
            with pytest.raises(Timeout), _try_file_lock(str(path), shared=True):
                pytest.fail("shared lock acquired an unlinked inode")
        with _try_file_lock(str(path), shared=True):
            with pytest.raises(Timeout), acquire_exclusive:
                pytest.fail("legacy writer acquired an active shared inode")
    finally:
        acquire_exclusive.release()


def test_unavailable_shared_flock_uses_exclusive_file_lock(tmp_path, monkeypatch):
    path = tmp_path / "directory.lock"
    flock = fcntl.flock

    def exclusive_only(fd, mode):
        if mode & fcntl.LOCK_SH:
            raise OSError(errno.ENOSYS, "shared flock unavailable")
        return flock(fd, mode)

    monkeypatch.setattr(fcntl, "flock", exclusive_only)
    acquire_exclusive = FileLock(path, timeout=0)
    with _try_file_lock(str(path), shared=True):
        with pytest.raises(Timeout), acquire_exclusive:
            pytest.fail("legacy writer acquired a fallback lock")
    with acquire_exclusive:
        pass


async def test_large_group_acquires_existing_flocks_in_path_order(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_local_file_lock_capacity", lambda: 2)
    monkeypatch.setattr(base, "HOST_WRITE_LOCK_TIMEOUT_SECONDS", 0.2)
    env = LocalEnvironment(str(tmp_path))
    first, blocked, last = (
        FileLock(base._host_lock_path(path, env), timeout=0) for path in ("a.txt", "b.txt", "c.txt")
    )
    other = FileLock(base._host_lock_path("other.txt", env), timeout=0)
    paths = ("c.txt", "a.txt", "b.txt")
    try:
        with blocked, pytest.raises(TimeoutError, match="b.txt"):
            async with base.host_write_locks(paths, env):
                pytest.fail("group acquired a lock already held by an existing writer")
        with first, last:
            pass
        async with base.host_write_locks(paths, env):
            for lock in (first, blocked, last):
                with pytest.raises(Timeout):
                    lock.acquire()
            with other:
                pass
    finally:
        await env.cleanup()


async def test_cancelled_acquisition_releases_the_partial_group(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_local_file_lock_capacity", lambda: 2)
    env = LocalEnvironment(str(tmp_path))
    paths = ("a.txt", "b.txt", "c.txt")
    first, blocked, last = (FileLock(base._host_lock_path(path, env), timeout=0) for path in paths)

    async def acquire_group():
        async with base.host_write_locks(paths, env):
            pytest.fail("group acquired a lock already held by an existing writer")

    async def wait_until_first_is_held():
        while True:
            try:
                with first:
                    pass
            except Timeout:
                return
            await asyncio.sleep(0.02)

    try:
        with blocked:
            owner = asyncio.create_task(acquire_group())
            try:
                await asyncio.wait_for(wait_until_first_is_held(), 5)
                with last:
                    pass
            finally:
                owner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await owner
            with first, last:
                pass
    finally:
        await env.cleanup()


def test_nested_file_write_with_four_free_descriptors(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-c",
         "import asyncio, sys; from tests.tools.test_local_file_lock_workers import _write_nested_file; "
         "asyncio.run(_write_nested_file(sys.argv[1]))", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        close_fds=True, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


async def _write_nested_file(workspace):
    target = Path(workspace, "nested/subdirectory/note.txt")
    target.parent.mkdir(parents=True)
    target.write_text("before\n")
    env = LocalEnvironment(workspace)
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = 256 if soft == resource.RLIM_INFINITY else min(soft, 256)
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
        opened = len(os.listdir("/dev/fd")) - 1
        with ExitStack() as descriptors:
            for _ in range(limit - opened - 4):
                descriptors.enter_context(open(os.devnull, "rb"))
            await FileWriteTool().execute_with_runtime(
                {"path": str(target.relative_to(workspace)), "mode": "str_replace",
                 "old_str": "before", "new_str": "after"},
                ToolRuntime(environment=env, safety_policy=None, permission_policy=None),
            )
        assert target.read_text() == "after\n"
    finally:
        await env.cleanup()


_OWNER = """
import asyncio
import sys

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools import base
from opencollab.application.async_timeout import await_owned_operation


async def main():
    base._local_file_lock_capacity = lambda: 2
    stop, workspace, *paths = sys.argv[1:]
    env = LocalEnvironment(workspace)
    entered = asyncio.Event()

    async def hold():
        async with base.host_write_locks(paths, env):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                if stop == 'terminal':
                    print('recovering', flush=True)
                    await await_owned_operation(asyncio.to_thread(sys.stdin.readline))

    task = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        print('ready', flush=True)
        if stop == 'terminal':
            await task
            return
        await asyncio.to_thread(sys.stdin.readline)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            print('cancelled', flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await env.cleanup()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    sys.exit(130)
"""


@pytest.mark.parametrize("stop", ["cancel", "crash", "terminal"])
async def test_large_group_releases_existing_flocks_after_owner_stops(tmp_path, stop):
    paths = ("a.txt", "b.txt", "c.txt")
    env = LocalEnvironment(str(tmp_path))
    old_locks = [FileLock(base._host_lock_path(path, env), timeout=2) for path in paths]
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _OWNER, stop, str(tmp_path), *paths,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )

    def confirm_released():
        for lock in old_locks:
            with lock:
                pass

    def confirm_held_during_recovery():
        with pytest.raises(Timeout), old_locks[0].acquire(timeout=0.25):
            pass

    try:
        assert await asyncio.wait_for(owner.stdout.readline(), 10) == b"ready\n"
        for lock in old_locks:
            with pytest.raises(Timeout):
                lock.acquire(timeout=0)
        if stop == "crash":
            owner.kill()
            await owner.wait()
        elif stop == "terminal":
            os.killpg(owner.pid, signal.SIGINT)
            assert await asyncio.wait_for(owner.stdout.readline(), 10) == b"recovering\n"
            await asyncio.to_thread(confirm_held_during_recovery)
            owner.stdin.write(b"finish\n")
            await owner.stdin.drain()
            assert await asyncio.wait_for(owner.wait(), 10) == 130
        else:
            owner.stdin.write(b"cancel\n")
            await owner.stdin.drain()
            assert await asyncio.wait_for(owner.stdout.readline(), 10) == b"cancelled\n"
            assert owner.returncode is None
        await asyncio.to_thread(confirm_released)
    finally:
        owner.stdin.close()
        if owner.returncode is None:
            owner.kill()
        await owner.wait()
        await env.cleanup()
