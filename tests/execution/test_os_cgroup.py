"""Real kernel cgroup checks through the delivered OSBackend."""
import asyncio
import os
import platform
import shlex
from pathlib import Path

import pytest

from opencollab.adapters.execution.linux import OSBackend

pytestmark = [
    pytest.mark.linux_only,
    pytest.mark.skipif(
        platform.system() != "Linux" or os.geteuid() != 0 or "EXECSERVER_TEST_IMAGE" not in os.environ,
        reason="cgroup tests need privileged Linux and EXECSERVER_TEST_IMAGE",
    ),
]


@pytest.fixture
def backend():
    return OSBackend()


@pytest.fixture
def image():
    return os.environ["EXECSERVER_TEST_IMAGE"]


def counters(path):
    return {key: int(value) for key, value in
            (line.split() for line in path.read_text().splitlines())}


async def test_init_and_exec_are_inside_limited_sandbox(backend, image):
    sb = await backend.create(image)
    try:
        cg = sb.env._sb.cgroup.path
        assert (cg / "cpu.max").read_text().strip() == "100000 100000"
        assert (cg / "memory.max").read_text().strip() == str(512 * 1024 * 1024)
        assert (cg / "pids.max").read_text().strip() == "128"
        relative = str(cg.relative_to("/sys/fs/cgroup"))
        assert relative in Path(f"/proc/{sb.env._sb.init_pid}/cgroup").read_text()
        result = await sb.env.exec_cmd("cat /proc/self/cgroup")
        assert result.returncode == 0
        assert relative in result.stdout
        assert relative not in Path("/proc/self/cgroup").read_text()
    finally:
        await backend.destroy(sb)


async def test_cpu_quota_really_throttles(backend, image, monkeypatch):
    monkeypatch.setenv("EXECSERVER_CPU_QUOTA_US", "10000")
    sb = await backend.create(image)
    try:
        stat = sb.env._sb.cgroup.path / "cpu.stat"
        before = counters(stat)
        code = "import time\nend=time.monotonic()+1\nwhile time.monotonic()<end: pass"
        result = await sb.env.exec_cmd("python3 -c " + shlex.quote(code))
        assert result.returncode == 0
        after = counters(stat)
        assert after["nr_throttled"] > before["nr_throttled"]
        assert after["throttled_usec"] > before["throttled_usec"]
    finally:
        await backend.destroy(sb)


async def test_pids_limit_rejects_forks(backend, image, monkeypatch):
    monkeypatch.setenv("EXECSERVER_PIDS_MAX", "16")
    sb = await backend.create(image)
    try:
        code = '''import errno, os, time
children=[]
try:
    for _ in range(100):
        try:
            pid=os.fork()
        except OSError as error:
            assert error.errno == errno.EAGAIN
            print("fork-limited", flush=True)
            break
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
'''
        result = await sb.env.exec_cmd("python3 -c " + shlex.quote(code))
        assert result.returncode == 0
        assert "fork-limited" in result.stdout
        assert counters(sb.env._sb.cgroup.path / "pids.events")["max"] > 0
    finally:
        await backend.destroy(sb)


async def test_memory_limit_kills_and_revokes_sandbox(backend, image, monkeypatch):
    monkeypatch.setenv("EXECSERVER_MEMORY_MAX_BYTES", str(64 * 1024 * 1024))
    sb = await backend.create(image)
    try:
        result = await sb.env.exec_cmd("python3 -c 'x=bytearray(256*1024*1024)'", timeout=20)
        assert result.returncode != 0
        assert "memory limit" in result.stderr
        assert sb.env.revoked
        assert counters(sb.env._sb.cgroup.path / "memory.events")["oom_kill"] > 0
    finally:
        await backend.destroy(sb)


async def test_destroy_kills_descendants_and_removes_group(backend, image):
    sb = await backend.create(image)
    cg = sb.env._sb.cgroup.path
    task = asyncio.create_task(sb.env.exec_cmd("sleep 60 & wait", timeout=90))
    try:
        for _ in range(100):
            if int((cg / "pids.current").read_text()) > 3:
                break
            await asyncio.sleep(0.02)
        assert int((cg / "pids.current").read_text()) > 3
        await backend.destroy(sb)
        await asyncio.gather(task, return_exceptions=True)
        assert not cg.exists()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await backend.destroy(sb)


async def test_missing_delegation_rejects_create(backend, image, monkeypatch):
    monkeypatch.delenv("EXECSERVER_CGROUP_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="CGROUP"):
        await backend.create(image)


async def test_missing_controllers_rejects_create(backend, image, monkeypatch):
    parent = Path(os.environ["EXECSERVER_CGROUP_ROOT"])
    undelegated = parent / "test-undelegated"
    undelegated.mkdir()
    monkeypatch.setenv("EXECSERVER_CGROUP_ROOT", str(undelegated))
    try:
        with pytest.raises(RuntimeError, match="delegate cpu, memory and pids"):
            await backend.create(image)
        assert not any(child.is_dir() for child in undelegated.iterdir())
    finally:
        undelegated.rmdir()


async def test_failed_namespace_setup_cleans_cgroup(backend, image, monkeypatch):
    parent = Path(os.environ["EXECSERVER_CGROUP_ROOT"])
    before = set(parent.iterdir())
    async def fail(sb):
        raise RuntimeError("injected namespace startup failure")
    monkeypatch.setattr(backend, "_launch_init", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await backend.create(image)
    assert set(parent.iterdir()) == before
