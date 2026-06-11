"""DockerEnvironment exec/attach behavior, verified against a fake docker shim.

No real docker or network: ``asyncio.create_subprocess_exec`` is replaced with a
fake that records argv and returns canned output, so we can assert the exact
``docker`` command lines for both the start-a-container path (harness) and the
attach path (SWE-bench eval).
"""

from __future__ import annotations

import asyncio

import opencollab.adapters.env as env_mod
from opencollab.adapters.env import DockerEnvironment


def run(coro):
    return asyncio.run(coro)


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr


class FakeDocker:
    """Records every ``create_subprocess_exec`` argv and replays queued procs."""

    def __init__(self, procs=None):
        self.calls = []
        self._procs = list(procs or [])

    async def __call__(self, *argv, stdout=None, stderr=None):
        self.calls.append(list(argv))
        if self._procs:
            return self._procs.pop(0)
        return FakeProc()


def patch_docker(monkeypatch, fake):
    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", fake)


def test_start_mode_setup_runs_container(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"cid123\n")])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(image="python:3.11-slim", workspace="/workspace")
    cid = run(env.setup(mount_dir="/abs/repo"))

    assert cid == "cid123"
    assert fake.calls == [
        [
            "docker", "run", "-d", "--rm",
            "-v", "/abs/repo:/workspace",
            "-w", "/workspace", "python:3.11-slim", "sleep", "infinity",
        ]
    ]


def test_start_mode_exec_unchanged(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"out", stderr=b"err", returncode=0)])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment()
    env._container_id = "cid123"
    result = run(env.exec_cmd("echo hi"))

    assert fake.calls == [
        ["docker", "exec", "cid123", "bash", "-c", "echo hi"]
    ]
    assert (result.returncode, result.stdout, result.stderr) == (0, "out", "err")


def test_exec_before_setup_raises(monkeypatch):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment()
    try:
        run(env.exec_cmd("echo hi"))
    except RuntimeError as e:
        assert "Container not started" in str(e)
    else:  # pragma: no cover - assertion path
        raise AssertionError("expected RuntimeError")
    assert fake.calls == []


def test_attach_mode_skips_setup(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"ok")])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="running_cid")
    result = run(env.exec_cmd("echo hi"))

    assert result.stdout == "ok"
    assert fake.calls == [
        ["docker", "exec", "running_cid", "bash", "-c", "echo hi"]
    ]


def test_attach_mode_cleanup_leaves_container_alone(monkeypatch):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="running_cid")
    run(env.cleanup())

    assert fake.calls == []
    assert env._container_id == "running_cid"


def test_start_mode_cleanup_kills_container(monkeypatch):
    fake = FakeDocker([FakeProc()])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment()
    env._container_id = "cid123"
    run(env.cleanup())

    assert fake.calls == [["docker", "kill", "cid123"]]
    assert env._container_id is None


def test_exec_workdir_adds_w_flag(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"out")])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="cid", exec_workdir="/testbed")
    run(env.exec_cmd("pytest"))

    assert fake.calls == [
        ["docker", "exec", "-w", "/testbed", "cid", "bash", "-c", "pytest"]
    ]


def test_string_command_prefix_uses_login_shell(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"out")])
    patch_docker(monkeypatch, fake)

    activate = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true"
    env = DockerEnvironment(
        container_id="cid", exec_workdir="/testbed", command_prefix=activate
    )
    run(env.exec_cmd("python -m pytest"))

    assert fake.calls == [
        [
            "docker", "exec", "-w", "/testbed", "cid",
            "bash", "-lc", f"{activate}\npython -m pytest",
        ]
    ]


def test_callable_command_prefix_wraps_command(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"out")])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(
        container_id="cid", command_prefix=lambda c: f"set -e; {c}"
    )
    run(env.exec_cmd("ls"))

    assert fake.calls == [
        ["docker", "exec", "cid", "bash", "-lc", "set -e; ls"]
    ]


def test_timeout_returns_default_negative_one(monkeypatch):
    fake = FakeDocker([FakeProc(hang=True)])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="cid")
    result = run(env.exec_cmd("sleep 999", timeout=0.01))

    assert result.returncode == -1
    assert "timed out after 0.01s" in result.stderr


def test_timeout_returncode_is_parameterized(monkeypatch):
    fake = FakeDocker([FakeProc(hang=True)])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="cid", timeout_returncode=124)
    result = run(env.exec_cmd("sleep 999", timeout=0.01))

    assert result.returncode == 124


def test_read_file_reads_via_cat(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"file body", returncode=0)])
    patch_docker(monkeypatch, fake)

    env = DockerEnvironment(container_id="cid")
    body = run(env.read_file("/testbed/a.py"))

    assert body == "file body"
    assert fake.calls == [
        ["docker", "exec", "cid", "bash", "-c", "cat -- /testbed/a.py"]
    ]
