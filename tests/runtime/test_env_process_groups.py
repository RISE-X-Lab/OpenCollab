"""Linux process-group exit detection and its conservative proc fallbacks."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from opencollab.adapters import _env_process as process_module


def _stat(pid=101, group=101, started=123, state=b"Z", name=b"parent") -> bytes:
    fields = [state, b"1", str(group).encode(), *([b"0"] * 16), str(started).encode()]
    return str(pid).encode() + b" (" + name + b") " + b" ".join(fields)


def _fake_proc(monkeypatch, entries, stats, *, mountinfo=None) -> None:
    if mountinfo is None:
        mountinfo = "1 0 0:1 / /proc rw,nosuid - proc proc rw\n"
    stats = {"self": _stat(os.getpid(), os.getpgrp()), **stats}
    original_listdir = os.listdir
    original_stat = os.stat

    def listdir(path):
        if path == "/proc":
            if isinstance(entries, BaseException):
                raise entries
            return entries() if callable(entries) else entries
        return original_listdir(path)

    def open_proc(path, *args, **kwargs):
        if path == "/proc/self/mountinfo":
            value = mountinfo() if callable(mountinfo) else mountinfo
            if isinstance(value, BaseException):
                raise value
            return io.StringIO(value)
        pid = path.removeprefix("/proc/").removesuffix("/stat")
        value = stats[pid]
        value = value() if callable(value) else value
        if isinstance(value, BaseException):
            raise value
        return io.BytesIO(value)

    def stat(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("/proc/"):
            return SimpleNamespace()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(process_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(process_module.os, "killpg", lambda _group, _signal: None)
    monkeypatch.setattr(process_module.os, "listdir", listdir)
    monkeypatch.setattr(process_module.os, "stat", stat)
    monkeypatch.setattr(process_module, "open", open_proc, raising=False)


@pytest.mark.parametrize("state", [b"Z", b"X", b"x"])
def test_linux_group_with_stable_exited_members_is_quiescent(monkeypatch, state) -> None:
    _fake_proc(monkeypatch, ["self", "101"], {"101": _stat(state=state, name=b"a (b) ) \xff")})

    assert process_module._group_exists(101) is False


@pytest.mark.parametrize("state", [b"R", b"S", b"D", b"T", b"t", b"I"])
def test_linux_group_with_active_and_zombie_members_remains_alive(monkeypatch, state) -> None:
    _fake_proc(monkeypatch, ["101", "102"], {"101": _stat(), "102": _stat(102, state=state)})

    assert process_module._group_exists(101) is True


@pytest.mark.parametrize("changed", ["pid", "started", "group", "state"])
def test_linux_group_identity_or_state_change_keeps_cleanup_running(monkeypatch, changed) -> None:
    second = {
        "pid": _stat(pid=102),
        "started": _stat(started=124),
        "group": _stat(group=102),
        "state": _stat(state=b"R"),
    }[changed]
    values = iter([_stat(), second])
    _fake_proc(monkeypatch, ["101"], {"101": lambda: next(values)})

    assert process_module._group_exists(101) is True


def test_linux_group_scan_catches_child_forked_after_first_enumeration(monkeypatch) -> None:
    entries = iter([["101"], ["101", "102"]])
    _fake_proc(
        monkeypatch,
        lambda: next(entries),
        {"101": _stat(), "102": _stat(102, state=b"S")},
    )

    assert process_module._group_exists(101) is True


def test_linux_group_new_exited_member_keeps_cleanup_running(monkeypatch) -> None:
    entries = iter([["101"], ["101", "102"]])
    _fake_proc(monkeypatch, lambda: next(entries), {"101": _stat(), "102": _stat(102)})

    assert process_module._group_exists(101) is True


def test_linux_group_visibility_change_keeps_cleanup_running(monkeypatch) -> None:
    mounts = iter([
        "1 0 0:1 / /proc rw - proc proc rw\n",
        "1 0 0:1 / /proc rw - proc proc rw,hidepid=2\n",
    ])
    _fake_proc(monkeypatch, ["101"], {"101": _stat()}, mountinfo=lambda: next(mounts))

    assert process_module._group_exists(101) is True


@pytest.mark.parametrize("error", [PermissionError(), OSError("read failure"), ValueError("invalid stat")])
@pytest.mark.parametrize("pid", ["101", "202"])
def test_linux_group_scan_failure_keeps_original_existence_result(monkeypatch, error, pid) -> None:
    _fake_proc(monkeypatch, ["101", "202"], {"101": _stat(), "202": _stat(202, group=202), pid: error})

    assert process_module._group_exists(101) is True


@pytest.mark.parametrize("value", [b"101 (parent) Z 1 101", b"broken", _stat(pid=102)])
def test_linux_group_incomplete_or_mismatched_stat_keeps_cleanup_running(monkeypatch, value) -> None:
    _fake_proc(monkeypatch, ["101"], {"101": value})

    assert process_module._group_exists(101) is True


def test_linux_group_with_no_visible_members_remains_alive(monkeypatch) -> None:
    _fake_proc(monkeypatch, ["self"], {})

    assert process_module._group_exists(101) is True


def test_linux_group_missing_stat_in_existing_directory_remains_unknown(monkeypatch) -> None:
    _fake_proc(monkeypatch, ["101"], {"101": FileNotFoundError()})

    assert process_module._group_exists(101) is True


def test_linux_group_allows_process_disappearing_during_scan(monkeypatch) -> None:
    _fake_proc(monkeypatch, ["101", "202"], {"101": _stat(), "202": FileNotFoundError()})
    original_stat = process_module.os.stat

    def stat(path, *args, **kwargs):
        if path == "/proc/202":
            raise FileNotFoundError()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(process_module.os, "stat", stat)

    assert process_module._group_exists(101) is False


@pytest.mark.parametrize(
    "mountinfo",
    [
        "1 0 0:1 / /proc rw - proc proc rw,hidepid=1\n",
        "1 0 0:1 / /proc rw,hidepid=2 - proc proc rw\n",
        "1 0 0:1 / /proc rw - proc proc rw,hidepid=4\n",
        "1 0 0:1 / /proc rw - tmpfs tmpfs rw\n",
        "1 0 0:1 /101 / /proc rw - proc proc rw\n",
        "1 0 0:1 / /proc rw - proc proc rw\n2 1 0:2 / /proc/102 rw - tmpfs tmpfs rw\n",
        "1 0 0:1 / /proc rw - proc proc rw\n2 1 0:2 / /proc/self rw - tmpfs tmpfs rw\n",
        "",
        "malformed\n",
        PermissionError(),
    ],
)
def test_linux_group_restricted_or_incomplete_mount_keeps_cleanup_running(monkeypatch, mountinfo) -> None:
    _fake_proc(monkeypatch, ["101"], {"101": _stat()}, mountinfo=mountinfo)

    assert process_module._group_exists(101) is True


def test_linux_group_proc_namespace_mismatch_remains_unknown(monkeypatch) -> None:
    _fake_proc(monkeypatch, ["101"], {"101": _stat(), "self": _stat(os.getpid() + 1, os.getpgrp())})

    assert process_module._group_exists(101) is True


def test_linux_group_enumeration_failure_remains_unknown(monkeypatch) -> None:
    _fake_proc(monkeypatch, PermissionError(), {})

    assert process_module._group_exists(101) is True


@pytest.mark.parametrize("error, exists", [(ProcessLookupError(), False), (PermissionError(), True)])
def test_group_signal_probe_preserves_lookup_and_permission_results(monkeypatch, error, exists) -> None:
    def killpg(_group, _signal):
        raise error

    monkeypatch.setattr(process_module.os, "killpg", killpg)
    monkeypatch.setattr(process_module, "_exited_proc_group_members", lambda _group: pytest.fail("unexpected scan"))

    assert process_module._group_exists(101) is exists


def test_non_linux_group_uses_existing_signal_probe(monkeypatch) -> None:
    monkeypatch.setattr(process_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(process_module.os, "killpg", lambda _group, _signal: None)
    monkeypatch.setattr(process_module, "_exited_proc_group_members", lambda _group: pytest.fail("unexpected scan"))

    assert process_module._group_exists(101) is True


_GROUP_SUPERVISOR = textwrap.dedent("""\
    import ctypes
    import os
    import pathlib
    import signal
    import sys
    import time

    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot become child subreaper')
    mode, workspace = sys.argv[1:]
    workspace = pathlib.Path(workspace)
    group = os.fork()
    if group == 0:
        os.setpgid(0, 0)
        (workspace / 'ready').touch()
        if mode == 'fork':
            while not (workspace / 'fork').exists():
                time.sleep(0.001)
        if mode != 'zombie':
            child = os.fork()
            if child == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                (workspace / 'child').write_text(str(os.getpid()))
                while True:
                    time.sleep(1)
        os._exit(0)
    print(group, flush=True)
    try:
        sys.stdin.readline()
    finally:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break
""")


async def _wait_until(predicate) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for controlled process state")


@pytest.fixture
async def linux_group(tmp_path):
    if sys.platform != "linux":
        pytest.skip("requires Linux proc and child subreaping")
    supervisors = []

    async def spawn(mode):
        workspace = tmp_path / str(len(supervisors))
        workspace.mkdir()
        supervisor = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _GROUP_SUPERVISOR, mode, str(workspace),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        supervisors.append(supervisor)
        group = int(await asyncio.wait_for(supervisor.stdout.readline(), timeout=5))
        await _wait_until(lambda: (workspace / "ready").exists())
        if mode != "fork":
            await _wait_until(lambda: process_module._read_proc_stat(str(group))[3] == b"Z")
        if mode == "mixed":
            await _wait_until(lambda: (workspace / "child").exists())
        return group, workspace

    try:
        yield spawn
    finally:
        for supervisor in supervisors:
            supervisor.stdin.write(b"reap\n")
            await supervisor.stdin.drain()
            _stdout, stderr = await asyncio.wait_for(supervisor.communicate(), timeout=5)
            assert supervisor.returncode == 0, stderr.decode()


async def test_linux_kernel_zombie_group_finishes_cleanup(linux_group) -> None:
    group, _workspace = await linux_group("zombie")
    os.killpg(group, 0)

    assert await process_module._wait_for_group_exit(group, timeout=0.1)


async def test_linux_kernel_mixed_group_waits_for_active_child(linux_group) -> None:
    group, _workspace = await linux_group("mixed")
    assert process_module._group_exists(group) is True
    assert await process_module._wait_for_group_exit(group, timeout=0.02) is False

    os.killpg(group, signal.SIGKILL)

    assert await process_module._wait_for_group_exit(group, timeout=1)


async def test_linux_kernel_fork_between_enumeration_and_stat_keeps_child_visible(linux_group, monkeypatch) -> None:
    group, workspace = await linux_group("fork")
    original_stat = process_module._read_proc_stat
    original_listdir = os.listdir
    first_snapshot = True

    def listdir(path):
        nonlocal first_snapshot
        entries = original_listdir(path)
        if path == "/proc" and first_snapshot:
            first_snapshot = False
            assert str(group) in entries
            (workspace / "fork").touch()
            for _ in range(500):
                if original_stat(str(group))[3] == b"Z" and (workspace / "child").exists():
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("controlled fork did not finish")
            assert (workspace / "child").read_text() not in entries
        return entries

    monkeypatch.setattr(process_module.os, "listdir", listdir)
    assert process_module._group_exists(group) is True
    assert first_snapshot is False
    child = int((workspace / "child").read_text())
    assert original_stat(str(child))[3] != b"Z"

    process = SimpleNamespace(pid=group, wait=lambda: asyncio.sleep(0))
    assert await process_module.terminate_process(process)
    assert original_stat(str(child))[3] in {b"Z", b"X"}


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux proc and child subreaping")
async def test_linux_local_environment_continues_after_zombie_descendant_cleanup(tmp_path) -> None:
    script = textwrap.dedent("""\
        import asyncio
        import ctypes
        import os
        import signal
        import sys
        from opencollab.adapters.env import LocalEnvironment

        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot become child subreaper')

        async def main():
            env = LocalEnvironment(sys.argv[1])
            try:
                first = await env.exec_cmd('sleep 30 >/dev/null 2>&1 & printf started')
                assert first.returncode == 0 and first.stdout == 'started'
                second = await env.exec_cmd('printf resumed')
                assert second.returncode == 0 and second.stdout == 'resumed'
                print('started resumed', flush=True)
            finally:
                await env.cleanup()

        try:
            asyncio.run(main())
        finally:
            for child in open(f'/proc/{os.getpid()}/task/{os.getpid()}/children').read().split():
                try:
                    os.kill(int(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            while True:
                try:
                    os.waitpid(-1, 0)
                except ChildProcessError:
                    break
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert process.returncode == 0, stderr.decode()
    assert stdout == b"started resumed\n"
