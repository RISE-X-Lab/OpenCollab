"""Docker environment ownership, compensation, and command-shape regressions."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import shlex
import subprocess
import sys
import threading
import time

import opencollab.adapters.env as env_mod
import pytest
from asyncio_test_support import assert_cancel_reason
from opencollab.adapters.env import DockerEnvironment

CONTAINER_ID_A = "a" * 64
CONTAINER_ID_B = "b" * 64


def run(coro):
    return asyncio.run(coro)


class _CaptureStdin(io.BytesIO):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def close(self):
        if not self.closed:
            self._owner.communicated_input = self.getvalue()
        super().close()


class FakeProc:
    # Keep synthetic process groups outside host PID ranges so group probes
    # cannot mistake an unrelated local process for a fake descendant.
    _next_pid = 1_000_000_000

    def __init__(
        self,
        stdout=b"",
        stderr=b"",
        returncode=0,
        *,
        hang=False,
        ignore_term=False,
        stdout_pipe=None,
    ):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.returncode = None if hang else returncode
        self._initial_stdout = stdout
        self._initial_stderr = stderr
        self._ignore_term = ignore_term
        self._stdout_pipe = stdout_pipe
        self.started = threading.Event()
        self.finished = threading.Event()
        self.killed = False
        self.terminated = False
        self.reaped = False
        self.communicated_input = None
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def bind(self, kwargs):
        self.stdin = (
            _CaptureStdin(self)
            if kwargs.get("stdin") == subprocess.PIPE
            else None
        )
        self.stdout = self._stdout_pipe or io.BytesIO(self._initial_stdout)
        self.stderr = io.BytesIO(self._initial_stderr)
        self.started.set()
        return self

    def poll(self):
        if self.returncode is not None:
            self.reaped = True
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and not self.finished.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        self.reaped = True
        return self.returncode

    def complete(self, returncode=0):
        self.returncode = returncode
        self.finished.set()

    def terminate(self):
        self.terminated = True
        if not self._ignore_term:
            self.complete(-15)

    def kill(self):
        self.killed = True
        self.complete(-9)


class FakeDocker:
    def __init__(self, procs=None, *, on_spawn=None, inspect_procs=None):
        self.calls = []
        self.call_kwargs = []
        self._procs = list(procs or [])
        self._inspect_procs = list(inspect_procs or [])
        self._on_spawn = on_spawn
        self._lock = threading.Lock()
        self._spawn_count = 0
        self.container_exists = False
        self.container_id = ""
        self.container_name = ""
        self.owner_token = ""

    def bind_owned(self, env, *, name="owned-name", container_id=None):
        container_id = container_id or hashlib.sha256(name.encode()).hexdigest()
        self.container_exists = True
        self.container_id = container_id
        self.container_name = name
        self.owner_token = env._owner_token
        env._container_id = container_id
        env._container_name = name

    def bind_foreign(self, *, name, owner_token="foreign-owner", container_id=None):
        self.container_exists = True
        self.container_id = container_id or hashlib.sha256(name.encode()).hexdigest()
        self.container_name = name
        self.owner_token = owner_token

    def _inspect_proc(self, command):
        reference = command[-1]
        inspect_format = command[command.index("--format") + 1]
        matches = self.container_exists and reference in {
            self.container_id,
            self.container_name,
        }
        if inspect_format == env_mod._DOCKER_ATTACH_INSPECT_FORMAT:
            if matches:
                container_id = self.container_id
                name = self.container_name
            elif re.fullmatch(r"[0-9a-fA-F]{64}", reference):
                container_id = reference.lower()
                name = "attached-by-id"
            else:
                container_id = hashlib.sha256(reference.encode()).hexdigest()
                name = reference
            return FakeProc(
                stdout=f"{container_id}\t/{name}\ttrue\n".encode()
            )
        if not matches:
            return FakeProc(returncode=1, stderr=b"No such container")
        return FakeProc(
            stdout=(
                f"{self.container_id}\t/{self.container_name}\t"
                f"{self.owner_token}\n"
            ).encode()
        )

    def _record_run_state(self, command, proc):
        if proc.returncode not in (None, 0):
            return
        name = command[command.index("--name") + 1]
        label = command[command.index("--label") + 1]
        token = label.split("=", 1)[1]
        try:
            candidate = proc._initial_stdout.decode("ascii").strip()
        except UnicodeDecodeError:
            candidate = ""
        self.container_exists = True
        self.container_id = (
            candidate
            if re.fullmatch(r"[0-9a-fA-F]{64}", candidate)
            else hashlib.sha256(name.encode()).hexdigest()
        )
        self.container_name = name
        self.owner_token = token

    def __call__(self, command, **kwargs):
        with self._lock:
            call = list(command) if not isinstance(command, str) else command
            self.calls.append(call)
            self.call_kwargs.append(dict(kwargs))
            is_inspect = isinstance(call, list) and call[:2] == ["docker", "inspect"]
            if is_inspect:
                proc = (
                    self._inspect_procs.pop(0)
                    if self._inspect_procs
                    else self._inspect_proc(call)
                )
                index = None
            else:
                proc = self._procs.pop(0) if self._procs else FakeProc()
                index = self._spawn_count
                self._spawn_count += 1
        proc.bind(kwargs)
        if isinstance(call, list) and call[:2] == ["docker", "run"]:
            self._record_run_state(call, proc)
        elif (
            isinstance(call, list)
            and call[:3] == ["docker", "rm", "-f"]
            and proc.returncode == 0
            and call[-1] in {self.container_id, self.container_name}
        ):
            self.container_exists = False
        if self._on_spawn is not None and index is not None:
            self._on_spawn(call, proc, index)
        return proc


def patch_docker(monkeypatch, fake):
    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", fake)


async def _wait_thread_event(event: threading.Event, timeout=0.5):
    assert await asyncio.to_thread(event.wait, timeout)


def test_start_mode_setup_runs_container(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=(CONTAINER_ID_A + "\n").encode())])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(
        DockerEnvironment,
        "_new_container_name",
        staticmethod(lambda: "opencollab-test-container"),
    )

    env = DockerEnvironment(image="python:3.11-slim", workspace="/workspace")
    cid = run(env.setup(mount_dir="/abs/repo"))

    assert cid == CONTAINER_ID_A
    assert fake.calls == [[
        "docker", "run", "-d", "--rm", "--name", "opencollab-test-container",
        "--network", "none",
        "--label", f"{env_mod.DOCKER_OWNER_LABEL}={env._owner_token}",
        "-v", "/abs/repo:/workspace", "-w", "/workspace",
        "--", "python:3.11-slim", "sleep", "infinity",
    ], [
        "docker", "inspect", "--type", "container", "--format",
        env_mod._DOCKER_INSPECT_FORMAT, "--", CONTAINER_ID_A,
    ]]
    assert fake.call_kwargs[0]["start_new_session"] is True


def test_start_mode_without_repo_mount_has_no_volume_argument(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=(CONTAINER_ID_B + "\n").encode())])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(
        DockerEnvironment,
        "_new_container_name",
        staticmethod(lambda: "opencollab-no-mount"),
    )

    run(DockerEnvironment(image="benchmark:latest").setup())

    assert fake.calls[0] == [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        "opencollab-no-mount",
        "--network",
        "none",
        "--label",
        f"{env_mod.DOCKER_OWNER_LABEL}={fake.owner_token}",
        "-w",
        "/workspace",
        "--",
        "benchmark:latest",
        "sleep",
        "infinity",
    ]
    assert "-v" not in fake.calls[0]


@pytest.mark.parametrize(
    "image",
    ["--privileged", "-v", "python:latest --privileged", "docker://python:3.11"],
)
def test_start_mode_rejects_option_like_or_malformed_image_before_spawn(
    monkeypatch,
    image,
):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)

    with pytest.raises(ValueError, match="image reference"):
        DockerEnvironment(image=image)

    assert fake.calls == []


def test_start_name_conflict_never_removes_foreign_container(monkeypatch):
    fake = FakeDocker([FakeProc(returncode=1, stderr=b"name already in use")])
    fake.bind_foreign(name="fixed-name")
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(
        DockerEnvironment,
        "_new_container_name",
        staticmethod(lambda: "fixed-name"),
    )
    env = DockerEnvironment()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        run(env.setup())

    assert env._aborted is True
    assert fake.container_exists is True
    assert not any(call[:3] == ["docker", "rm", "-f"] for call in fake.calls)


def test_attached_container_rejects_owned_backing_environment():
    with pytest.raises(ValueError, match="attached Docker environment"):
        DockerEnvironment(
            container_id="existing",
            backing_environment=env_mod.Environment(),
        )


def test_only_docker_environment_declares_process_isolation():
    assert env_mod.Environment.process_isolated is False
    assert env_mod.LocalEnvironment.process_isolated is False
    assert env_mod.WorktreeEnvironment.process_isolated is False
    assert DockerEnvironment.process_isolated is True


@pytest.mark.parametrize(
    "reference",
    ["-v", "--privileged", "abc123", "a" * 63, "bad/name", "x" * 256],
)
def test_attached_container_rejects_option_like_or_ambiguous_reference(reference):
    with pytest.raises(ValueError, match="container reference"):
        DockerEnvironment(container_id=reference)


def test_attached_name_binds_once_then_executes_only_by_full_id(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"one"), FakeProc(stdout=b"two")])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="stable-name")

    first = run(env.exec_cmd("first"))
    second = run(env.exec_cmd("second"))

    full_id = hashlib.sha256(b"stable-name").hexdigest()
    inspect_calls = [call for call in fake.calls if call[:2] == ["docker", "inspect"]]
    exec_calls = [call for call in fake.calls if call[:2] == ["docker", "exec"]]
    assert inspect_calls == [[
        "docker", "inspect", "--type", "container", "--format",
        env_mod._DOCKER_ATTACH_INSPECT_FORMAT, "--", "stable-name",
    ]]
    assert all(call[call.index("--") + 1] == full_id for call in exec_calls)
    assert (first.stdout, second.stdout) == ("one", "two")


def test_attached_name_change_during_binding_fails_before_exec(monkeypatch):
    inspect = FakeProc(
        stdout=(f"{CONTAINER_ID_A}\t/replacement-name\ttrue\n").encode()
    )
    fake = FakeDocker([FakeProc(stdout=b"must not run")], inspect_procs=[inspect])
    patch_docker(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="ambiguous or changed"):
        run(DockerEnvironment(container_id="expected-name").exec_cmd("unsafe"))

    assert not any(call[:2] == ["docker", "exec"] for call in fake.calls)


def test_attached_full_id_mismatch_fails_before_exec(monkeypatch):
    inspect = FakeProc(
        stdout=(f"{CONTAINER_ID_B}\t/anything\ttrue\n").encode()
    )
    fake = FakeDocker([FakeProc(stdout=b"must not run")], inspect_procs=[inspect])
    patch_docker(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="ambiguous or changed"):
        run(DockerEnvironment(container_id=CONTAINER_ID_A).exec_cmd("unsafe"))

    assert not any(call[:2] == ["docker", "exec"] for call in fake.calls)


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_setup_invalid_timeout_has_no_name_or_spawn(monkeypatch, invalid_timeout):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "DOCKER_SETUP_TIMEOUT_SECONDS", invalid_timeout)
    env = DockerEnvironment()

    with pytest.raises(ValueError, match="positive finite"):
        run(env.setup())

    assert env._container_name is None
    assert fake.calls == []


def test_cancelled_setup_removes_only_the_inspected_full_id(monkeypatch):
    run_proc = FakeProc(
        stdout=(CONTAINER_ID_A + "\n").encode(),
        hang=True,
        ignore_term=True,
    )
    fake = FakeDocker([run_proc])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)

    compensation_calls = 0

    async def scenario():
        nonlocal compensation_calls
        env = DockerEnvironment()
        original_compensation = env._sync_compensate_failed_setup

        def counted_compensation(container_name, stdout):
            nonlocal compensation_calls
            compensation_calls += 1
            original_compensation(container_name, stdout)

        monkeypatch.setattr(env, "_sync_compensate_failed_setup", counted_compensation)
        task = asyncio.create_task(env.setup())
        await _wait_thread_event(run_proc.started)
        task.cancel("cancel docker run")
        with pytest.raises(asyncio.CancelledError):
            await task
        return env

    env = run(scenario())
    refs = [call[-1] for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]
    assert refs == [CONTAINER_ID_A]
    assert run_proc.killed is True
    assert run_proc.reaped is True
    assert env._container_id is None
    assert env._container_name is None
    assert compensation_calls == 1


def test_cancellation_after_run_completion_still_compensates(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=(CONTAINER_ID_B + "\n").encode())])
    patch_docker(monkeypatch, fake)
    original_wait = env_mod._wait_thread_event
    inject_cancel = True

    async def cancel_at_handoff(event, *, timeout):
        nonlocal inject_cancel
        completed = await original_wait(event, timeout=timeout)
        if inject_cancel:
            inject_cancel = False
            asyncio.current_task().cancel("handoff cancellation")
            await asyncio.sleep(0)
        return completed

    monkeypatch.setattr(env_mod, "_wait_thread_event", cancel_at_handoff)
    env = DockerEnvironment()
    compensation_calls = 0
    ticker_count = 0
    ticker_stop = asyncio.Event()
    original_compensation = env._sync_compensate_failed_setup

    def counted_compensation(container_name, stdout):
        nonlocal compensation_calls
        compensation_calls += 1
        time.sleep(0.08)
        original_compensation(container_name, stdout)

    monkeypatch.setattr(env, "_sync_compensate_failed_setup", counted_compensation)

    async def ticker():
        nonlocal ticker_count
        while not ticker_stop.is_set():
            ticker_count += 1
            await asyncio.sleep(0.005)

    async def scenario():
        ticker_task = asyncio.create_task(ticker())
        try:
            with pytest.raises(asyncio.CancelledError):
                await env.setup()
        finally:
            ticker_stop.set()
            await ticker_task

    run(scenario())

    rm_refs = [call[-1] for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]
    assert rm_refs == [CONTAINER_ID_B]
    assert env._container_id is None
    assert env._container_name is None
    assert compensation_calls == 1
    assert ticker_count >= 5


def test_setup_compensation_failure_retains_recovery_refs(monkeypatch):
    run_proc = FakeProc(
        stdout=(CONTAINER_ID_A + "\n").encode(),
        hang=True,
        ignore_term=True,
    )
    failed_rm = [FakeProc(returncode=1, stderr=b"daemon unavailable")]
    fake = FakeDocker([run_proc, *failed_rm])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment()

    async def scenario():
        task = asyncio.create_task(env.setup())
        await _wait_thread_event(run_proc.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() is True

    run(scenario())
    assert env._container_id == CONTAINER_ID_A
    assert env._container_name is not None


@pytest.mark.parametrize("case", ["empty-id", "truncated-id"])
def test_setup_result_failure_plus_remove_failure_revokes_and_retains_refs(
    monkeypatch,
    case,
):
    if case == "empty-id":
        run_proc = FakeProc(stdout=b"")
    else:
        monkeypatch.setattr(env_mod, "PROCESS_OUTPUT_CAPTURE_BYTES", 256)
        run_proc = FakeProc(stdout=b"x" * 400)
    fake = FakeDocker([
        run_proc,
        FakeProc(returncode=1, stderr=b"remove failed"),
    ])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(
        DockerEnvironment,
        "_new_container_name",
        staticmethod(lambda: "retained-name"),
    )
    env = DockerEnvironment()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        run(env.setup())

    assert env._aborted is True
    assert env._container_name == "retained-name"
    rm_calls = [call for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]
    assert rm_calls == [["docker", "rm", "-f", fake.container_id]]


def test_setup_nonzero_without_owned_container_does_not_remove_name(monkeypatch):
    fake = FakeDocker([FakeProc(returncode=1, stderr=b"name conflict")])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(
        DockerEnvironment,
        "_new_container_name",
        staticmethod(lambda: "conflicting-name"),
    )

    with pytest.raises(RuntimeError, match="name conflict"):
        run(DockerEnvironment().setup())

    assert not any(call[:3] == ["docker", "rm", "-f"] for call in fake.calls)


def test_setup_names_are_unique():
    assert DockerEnvironment._new_container_name() != DockerEnvironment._new_container_name()


def test_exec_shape_workdir_and_prefix(monkeypatch):
    fake = FakeDocker([FakeProc(stdout=b"out", stderr=b"err")])
    patch_docker(monkeypatch, fake)
    activate = "source /opt/activate testbed"
    env = DockerEnvironment(
        container_id="cid",
        exec_workdir="/testbed",
        command_prefix=activate,
    )

    result = run(env.exec_cmd("python -m pytest"))

    call = next(call for call in fake.calls if call[:2] == ["docker", "exec"])
    full_id = hashlib.sha256(b"cid").hexdigest()
    assert call[:8] == [
        "docker", "exec", "-w", "/testbed", "--", full_id, "bash", "-c"
    ]
    assert call[8] == env_mod._DOCKER_EXEC_WRAPPER
    assert call[-2:] == ["-lc", f"{activate}\npython -m pytest"]
    assert (result.returncode, result.stdout, result.stderr) == (0, "out", "err")


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_exec_invalid_timeout_never_spawns(monkeypatch, invalid_timeout):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")
    with pytest.raises(ValueError, match="positive finite"):
        run(env.exec_cmd("side effect", timeout=invalid_timeout))
    assert fake.calls == []


def test_exec_large_output_is_bounded(monkeypatch):
    monkeypatch.setattr(env_mod, "PROCESS_OUTPUT_CAPTURE_BYTES", 1024)
    fake = FakeDocker([FakeProc(stdout=b"a" * 10_000, stderr=b"b" * 12_000)])
    patch_docker(monkeypatch, fake)
    result = run(DockerEnvironment(container_id="cid").exec_cmd("large"))

    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert result.stdout_dropped_bytes == 8976
    assert result.stderr_dropped_bytes == 10976
    assert len(result.stdout.encode()) < 1200
    assert len(result.stderr.encode()) < 1200


def test_timeout_stops_remote_inner_process(monkeypatch):
    remote_cancelled = threading.Event()
    sentinel = threading.Event()
    outer = FakeProc(hang=True, ignore_term=True)

    def on_spawn(call, _proc, index):
        if index == 0:
            threading.Timer(
                0.12,
                lambda: None if remote_cancelled.is_set() else sentinel.set(),
            ).start()
        elif env_mod._DOCKER_EXEC_CANCEL in call:
            remote_cancelled.set()

    fake = FakeDocker([outer, FakeProc()], on_spawn=on_spawn)
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment(container_id="cid", timeout_returncode=124)

    result = run(env.exec_cmd("remote work", timeout=0.02))
    time.sleep(0.15)

    assert result.returncode == 124
    assert remote_cancelled.is_set()
    assert not sentinel.is_set()
    assert outer.killed and outer.reaped


def test_attached_timeout_marker_failure_is_explicit(monkeypatch):
    outer = FakeProc(hang=True, ignore_term=True)
    fake = FakeDocker([outer, FakeProc(returncode=125, stderr=b"marker failed")])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="could not be terminated"):
        run(env.exec_cmd("remote work", timeout=0.01))
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))


def test_cancel_inner_cleanup_failure_revokes_attached_environment(monkeypatch):
    outer = FakeProc(hang=True)
    inner_cancel = FakeProc(returncode=125, stderr=b"marker failed")
    fake = FakeDocker([outer, inner_cancel])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    async def scenario():
        task = asyncio.create_task(env.exec_cmd("remote", timeout=60))
        await _wait_thread_event(outer.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))


def test_transport_error_inner_cleanup_failure_revokes_attached_environment(
    monkeypatch,
):
    outer = FakeProc(hang=True, stdout_pipe=_ExplodingPipe())
    fake = FakeDocker(
        [outer, FakeProc(returncode=125, stderr=b"marker failed")]
    )
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="transport failed"):
        run(env.exec_cmd("remote", timeout=2))
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))


def test_owned_timeout_falls_back_to_container_removal(monkeypatch):
    outer = FakeProc(hang=True, ignore_term=True)
    fake = FakeDocker([
        outer,
        FakeProc(returncode=124, stderr=b"no pidfile"),
        FakeProc(),
    ])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment()
    fake.bind_owned(env)

    result = run(env.exec_cmd("remote work", timeout=0.01))

    assert result.returncode == -1
    rm_calls = [call for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]
    assert rm_calls == [["docker", "rm", "-f", fake.container_id]]
    assert env._container_id is None
    assert env._container_name is None


def test_double_cancel_preserves_first_cancellation_and_finishes_inner_cleanup(
    monkeypatch,
):
    outer = FakeProc(hang=True, ignore_term=True)
    cancel_proc = FakeProc(hang=True)
    fake = FakeDocker([outer, cancel_proc, FakeProc(stdout=b"reusable")])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment(container_id="attached")

    async def scenario():
        task = asyncio.create_task(env.exec_cmd("remote", timeout=60))
        await _wait_thread_event(outer.started)
        task.cancel("first cancellation")
        await _wait_thread_event(cancel_proc.started)
        task.cancel("second cancellation")
        threading.Timer(0.03, lambda: cancel_proc.complete(0)).start()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert_cancel_reason(captured.value, "first cancellation")
        assert task.cancelled() is True
        followup = await env.exec_cmd("echo reusable", timeout=1)
        assert followup.stdout == "reusable"

    run(scenario())
    assert cancel_proc.reaped is True


class _ExplodingPipe:
    def read(self, _size):
        raise OSError("docker CLI transport failed")

    def close(self):
        return None


def test_transport_error_always_attempts_inner_cancel(monkeypatch):
    outer = FakeProc(hang=True, stdout_pipe=_ExplodingPipe())
    fake = FakeDocker([outer, FakeProc()])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="transport failed"):
        run(env.exec_cmd("remote", timeout=2))

    assert len([call for call in fake.calls if call[:2] == ["docker", "exec"]]) == 2
    assert any(env_mod._DOCKER_EXEC_CANCEL in call for call in fake.calls)


def test_quiescence_failure_return_code_forces_inner_cleanup(monkeypatch):
    outer = FakeProc(
        returncode=env_mod.DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE
    )
    fake = FakeDocker([outer, FakeProc()])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="descendants required forced cleanup"):
        run(env.exec_cmd("background child", timeout=2))

    assert any(env_mod._DOCKER_EXEC_CANCEL in call for call in fake.calls[1:])


def test_not_quiesced_revokes_attached_environment(monkeypatch):
    outer = FakeProc(hang=True, stdout_pipe=_ExplodingPipe())
    fake = FakeDocker([outer, FakeProc()])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", lambda proc: False)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(env_mod._OwnedProcessNotQuiesced):
        run(env.exec_cmd("remote", timeout=2))
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))
    assert len([call for call in fake.calls if call[:2] == ["docker", "exec"]]) == 2


def test_cancel_cleanup_failure_revokes_attached_environment(monkeypatch):
    outer = FakeProc(hang=True)
    fake = FakeDocker([outer, FakeProc()])
    patch_docker(monkeypatch, fake)
    real_cleanup = env_mod._sync_terminate_process_group

    def fail_outer_cleanup(proc):
        if proc is outer:
            proc.kill()
            proc.wait(timeout=0.1)
            return False
        return real_cleanup(proc)

    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", fail_outer_cleanup)
    env = DockerEnvironment(container_id="attached")

    async def scenario():
        task = asyncio.create_task(env.exec_cmd("remote", timeout=60))
        await _wait_thread_event(outer.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))


def test_timeout_cleanup_failure_revokes_attached_environment(monkeypatch):
    outer = FakeProc(hang=True)
    fake = FakeDocker([outer, FakeProc()])
    patch_docker(monkeypatch, fake)

    def fail_outer_cleanup(proc):
        proc.kill()
        proc.wait(timeout=0.1)
        return False

    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", fail_outer_cleanup)
    env = DockerEnvironment(container_id="attached")

    result = run(env.exec_cmd("remote", timeout=0.01))
    assert result.returncode == -1
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.exec_cmd("must not spawn", timeout=1))


def test_cleanup_retry_state_and_attach_cleanup(monkeypatch):
    fake = FakeDocker([
        FakeProc(returncode=1, stderr=b"daemon unavailable"),
        FakeProc(),
    ])
    patch_docker(monkeypatch, fake)
    owned = DockerEnvironment()
    fake.bind_owned(owned)

    with pytest.raises(OSError, match="cleanup failed"):
        run(owned.cleanup())
    assert owned._container_id == fake.container_id
    assert owned._container_name == "owned-name"
    run(owned.cleanup())
    assert owned._container_id is None
    assert owned._container_name is None

    attached = DockerEnvironment(container_id="attached")
    run(attached.cleanup())
    assert len([call for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]) == 2


class _BackingEnvironment(env_mod.Environment):
    def __init__(
        self,
        *,
        cleanup_failures: list[BaseException | None] | None = None,
        abort_failure: BaseException | None = None,
    ):
        self.cleanup_failures = list(cleanup_failures or [])
        self.abort_failure = abort_failure
        self.cleanup_calls = 0
        self.abort_calls = 0

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        failure = self.cleanup_failures.pop(0) if self.cleanup_failures else None
        if failure is not None:
            raise failure

    async def abort(self) -> None:
        self.abort_calls += 1
        if self.abort_failure is not None:
            raise self.abort_failure


@pytest.mark.asyncio
async def test_cleanup_finishes_backing_environment_before_propagating_cancel():
    class SlowBacking(_BackingEnvironment):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def cleanup(self) -> None:
            self.cleanup_calls += 1
            self.started.set()
            await self.release.wait()

    backing = SlowBacking()
    env = DockerEnvironment(backing_environment=backing)
    task = asyncio.create_task(env.cleanup())
    await backing.started.wait()

    task.cancel("Docker cleanup cancelled")
    await asyncio.sleep(0)
    assert task.done() is False

    backing.release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert_cancel_reason(raised.value, "Docker cleanup cancelled")
    assert backing.cleanup_calls == 1
    assert env._backing_environment is None


def test_abort_retries_backing_cleanup_after_initial_teardown_failures(
    monkeypatch,
):
    fake = FakeDocker(
        [
            FakeProc(returncode=1, stderr=b"daemon unavailable"),
            FakeProc(),
        ]
    )
    patch_docker(monkeypatch, fake)
    backing = _BackingEnvironment(
        cleanup_failures=[OSError("worktree busy"), None]
    )
    env = DockerEnvironment(backing_environment=backing)
    fake.bind_owned(env)

    with pytest.raises(OSError, match="cleanup failed"):
        run(env.cleanup())

    assert backing.cleanup_calls == 1
    assert env._backing_environment is backing

    run(env.abort())

    assert backing.abort_calls == 1
    assert backing.cleanup_calls == 2
    assert env._backing_environment is None
    assert env._container_id is None


def test_cleanup_attempts_backing_when_container_and_backing_both_fail(monkeypatch):
    fake = FakeDocker([FakeProc(returncode=1, stderr=b"container failed")])
    patch_docker(monkeypatch, fake)
    backing = _BackingEnvironment(
        cleanup_failures=[OSError("backing failed")]
    )
    env = DockerEnvironment(backing_environment=backing)
    fake.bind_owned(env)

    with pytest.raises(OSError) as raised:
        run(env.cleanup())

    assert len([call for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]) == 1
    assert backing.cleanup_calls == 1
    assert env._backing_environment is backing
    assert any("backing cleanup" in note for note in raised.value.__notes__)


def test_abort_attempts_all_teardown_stages_when_each_fails(monkeypatch):
    fake = FakeDocker([FakeProc(returncode=1, stderr=b"container failed")])
    patch_docker(monkeypatch, fake)
    backing = _BackingEnvironment(
        cleanup_failures=[OSError("backing cleanup failed")],
        abort_failure=OSError("backing abort failed"),
    )
    env = DockerEnvironment(backing_environment=backing)
    fake.bind_owned(env)

    with pytest.raises(OSError) as raised:
        run(env.abort())

    assert len([call for call in fake.calls if call[:3] == ["docker", "rm", "-f"]]) == 1
    assert backing.abort_calls == 1
    assert backing.cleanup_calls == 1
    assert env._backing_environment is backing
    assert len(raised.value.__notes__) >= 3


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_write_invalid_timeout_never_spawns(monkeypatch, invalid_timeout):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "DOCKER_WRITE_TIMEOUT_SECONDS", invalid_timeout)
    env = DockerEnvironment(container_id="cid")
    with pytest.raises(ValueError, match="positive finite"):
        run(env.write_file("x", "side effect"))
    assert fake.calls == []


def test_write_timeout_inner_cleanup_failure_revokes_attached_environment(
    monkeypatch,
):
    outer = FakeProc(hang=True)
    fake = FakeDocker(
        [outer, FakeProc(returncode=125, stderr=b"marker failed")]
    )
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(env_mod, "DOCKER_WRITE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.01)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="could not be terminated"):
        run(env.write_file("x", "payload"))
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.write_file("again", "payload"))


def test_cancelled_write_inner_cleanup_failure_revokes_attached_environment(
    monkeypatch,
):
    outer = FakeProc(hang=True)
    fake = FakeDocker(
        [outer, FakeProc(returncode=125, stderr=b"marker failed")]
    )
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    async def scenario():
        task = asyncio.create_task(env.write_file("x", "payload"))
        await _wait_thread_event(outer.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.write_file("again", "payload"))


def test_write_transport_cleanup_failure_revokes_attached_environment(
    monkeypatch,
):
    outer = FakeProc(hang=True, stdout_pipe=_ExplodingPipe())
    fake = FakeDocker(
        [outer, FakeProc(returncode=125, stderr=b"marker failed")]
    )
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="attached")

    with pytest.raises(OSError, match="transport failed"):
        run(env.write_file("x", "payload"))
    with pytest.raises(RuntimeError, match="aborted"):
        run(env.write_file("again", "payload"))


def test_write_streams_stdin_then_verifies(monkeypatch):
    content = "x" * 20_000
    verification = (
        f"{len(content.encode())}\t"
        f"{hashlib.sha256(content.encode()).hexdigest()}\n"
    ).encode()
    write_proc = FakeProc(stdout=verification)
    fake = FakeDocker([write_proc])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid", exec_workdir="/testbed")

    run(env.write_file("big.txt", content))

    exec_call = next(call for call in fake.calls if call[:2] == ["docker", "exec"])
    assert exec_call[:9] == [
        "docker", "exec", "-i", "-w", "/testbed", "--",
        hashlib.sha256(b"cid").hexdigest(), "bash", "-c",
    ]
    assert write_proc.communicated_input == content.encode()
    assert content not in " ".join(fake.calls[0])
    assert len([call for call in fake.calls if call[:2] == ["docker", "exec"]]) == 1


def test_write_larger_than_capture_limit_uses_small_digest_verification(monkeypatch):
    content = "€" * 500_000
    payload = content.encode("utf-8")
    assert len(payload) > env_mod.PROCESS_OUTPUT_CAPTURE_BYTES
    verification = (
        f"{len(payload)}\t{hashlib.sha256(payload).hexdigest()}\n"
    ).encode()
    write_proc = FakeProc(stdout=verification)
    fake = FakeDocker([write_proc])
    patch_docker(monkeypatch, fake)

    run(DockerEnvironment(container_id="cid").write_file("large.txt", content))

    assert write_proc.communicated_input == payload
    assert len([call for call in fake.calls if call[:2] == ["docker", "exec"]]) == 1


def test_write_verifier_script_handles_large_utf8_payload(tmp_path):
    payload = ("restored patch €\n" * 180_000).encode("utf-8")
    assert len(payload) > env_mod.PROCESS_OUTPUT_CAPTURE_BYTES
    target = tmp_path / "large payload.txt"

    result = subprocess.run(
        [
            "bash",
            "-c",
            env_mod._DOCKER_WRITE_AND_VERIFY,
            "opencollab-write",
            str(target),
        ],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == (
        f"{len(payload)}\t{hashlib.sha256(payload).hexdigest()}"
    )
    assert target.read_bytes() == payload


def test_docker_temp_file_creation_is_unique_and_digest_verified(monkeypatch):
    content = "checkpoint\n" * 100
    payload = content.encode()
    temp_path = "/tmp/opencollab-checkpoint-recovery-A1b2C3d4.patch"
    verification = (
        f"7:11\t{len(payload)}\t{hashlib.sha256(payload).hexdigest()}\n"
    ).encode()
    monkeypatch.setattr(
        env_mod.uuid,
        "uuid4",
        lambda: type("FixedUUID", (), {"hex": "A1b2C3d4"})(),
    )
    write_proc = FakeProc(stdout=verification)
    fake = FakeDocker([write_proc])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")

    created = run(
        env.write_temp_file(
            content,
            prefix="opencollab-checkpoint-recovery-",
            suffix=".patch",
        )
    )

    assert created == temp_path
    exec_calls = [call for call in fake.calls if call[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 1
    assert env_mod._DOCKER_CREATE_WRITE_AND_VERIFY in exec_calls[0]
    assert temp_path in exec_calls[0]
    assert write_proc.communicated_input == payload
    assert env._docker_temp_file_identities[temp_path] == "7:11"


def test_docker_temp_write_failure_without_identity_never_blindly_removes_path(monkeypatch):
    temp_path = "/tmp/opencollab-test-patch-owned.diff"
    monkeypatch.setattr(
        env_mod.uuid,
        "uuid4",
        lambda: type("FixedUUID", (), {"hex": "owned"})(),
    )
    failed_write = FakeProc(returncode=74, stderr=b"write failed")
    fake = FakeDocker([failed_write])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")

    with pytest.raises(OSError, match="docker temporary write failed"):
        run(
            env.write_temp_file(
                "patch",
                prefix="opencollab-test-patch-",
                suffix=".diff",
            )
        )

    exec_calls = [call for call in fake.calls if call[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 1
    assert env_mod._DOCKER_CREATE_WRITE_AND_VERIFY in exec_calls[0]
    assert temp_path not in getattr(env, "_docker_temp_file_identities", {})


def test_docker_temp_verification_failure_removes_only_proven_identity(monkeypatch):
    temp_path = "/tmp/opencollab-test-patch-verified.diff"
    monkeypatch.setattr(
        env_mod.uuid,
        "uuid4",
        lambda: type("FixedUUID", (), {"hex": "verified"})(),
    )
    bad_verification = FakeProc(stdout=b"7:11\t5\tbad-digest\n")
    verified_remove = FakeProc()
    fake = FakeDocker([bad_verification, verified_remove])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")

    with pytest.raises(OSError, match="verification failed"):
        run(
            env.write_temp_file(
                "patch",
                prefix="opencollab-test-patch-",
                suffix=".diff",
            )
        )

    exec_calls = [call for call in fake.calls if call[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 2
    removal = " ".join(exec_calls[1])
    assert temp_path in removal
    assert "7:11" in removal
    assert temp_path not in env._docker_temp_file_identities


def test_docker_remove_temp_requires_known_identity_without_spawning(monkeypatch):
    fake = FakeDocker()
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")

    with pytest.raises(OSError, match="without ownership proof"):
        run(env.remove_file("/tmp/unknown.diff"))

    assert fake.calls == []


def test_docker_remove_temp_compares_remote_identity(monkeypatch):
    path = "/tmp/owned.diff"
    fake = FakeDocker([FakeProc(returncode=76)])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")
    env._docker_temp_file_identities = {path: "7:11"}

    with pytest.raises(OSError, match="replaced container temporary file"):
        run(env.remove_file(path))

    exec_calls = [call for call in fake.calls if call[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 1
    joined = " ".join(exec_calls[0])
    assert "stat -c" in joined
    assert path in joined
    assert "7:11" in joined
    assert path in env._docker_temp_file_identities


def test_docker_remove_temp_forgets_identity_only_after_verified_removal(monkeypatch):
    path = "/tmp/owned.diff"
    fake = FakeDocker([FakeProc()])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")
    env._docker_temp_file_identities = {path: "7:11"}

    run(env.remove_file(path))

    assert path not in env._docker_temp_file_identities


def test_docker_retirement_limit_failure_keeps_cleanup_identity(monkeypatch):
    path = "/tmp/owned.diff"
    fake = FakeDocker([FakeProc(returncode=78)])
    patch_docker(monkeypatch, fake)
    env = DockerEnvironment(container_id="cid")
    env._docker_temp_file_identities = {path: "7:11"}

    with pytest.raises(OSError, match="failed to remove container temporary file"):
        run(env.remove_file(path))

    assert path in env._docker_temp_file_identities
    assert "-ge 256" in env_mod._DOCKER_REMOVE_OWNED_TEMP
    assert "mv -T -n --" in env_mod._DOCKER_REMOVE_OWNED_TEMP
    assert "flock -x 9" in env_mod._DOCKER_REMOVE_OWNED_TEMP


@pytest.mark.skipif(sys.platform != "linux", reason="Docker control script uses Linux /proc and stat")
def test_docker_create_write_script_detects_name_swap_without_touching_victim(tmp_path):
    target = tmp_path / "owned.tmp"
    detached = tmp_path / "detached.tmp"
    victim = tmp_path / "victim.txt"
    victim.write_text("victim", encoding="utf-8")
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            env_mod._DOCKER_CREATE_WRITE_AND_VERIFY,
            "opencollab-write",
            str(target),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 2
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert target.exists()
    target.rename(detached)
    target.symlink_to(victim)

    stdout, stderr = process.communicate(b"payload", timeout=5)

    assert process.returncode == 75, (stdout, stderr)
    assert detached.read_bytes() == b"payload"
    assert target.is_symlink() and target.resolve() == victim
    assert victim.read_text(encoding="utf-8") == "victim"
    assert not list(tmp_path.glob(".opencollab-retired-*"))


@pytest.mark.skipif(sys.platform != "linux", reason="Docker control script uses GNU stat")
def test_docker_remove_owned_script_refuses_replacement_without_touching_victim(tmp_path):
    target = tmp_path / "owned.tmp"
    target.write_text("owned", encoding="utf-8")
    opened = target.stat()
    expected = f"{opened.st_dev}:{opened.st_ino}"
    target.unlink()
    victim = tmp_path / "victim.txt"
    victim.write_text("victim", encoding="utf-8")
    target.symlink_to(victim)

    result = subprocess.run(
        [
            "bash",
            "-c",
            env_mod._DOCKER_REMOVE_OWNED_TEMP,
            "opencollab-remove",
            str(target),
            expected,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 76
    assert target.is_symlink() and target.resolve() == victim
    assert victim.read_text(encoding="utf-8") == "victim"
    assert not list(tmp_path.glob(".opencollab-retired-*"))


@pytest.mark.skipif(sys.platform != "linux", reason="Docker control script uses GNU stat and mv")
def test_docker_remove_retires_owned_inode_without_final_rm(tmp_path):
    target = tmp_path / "owned.tmp"
    target.write_text("owned", encoding="utf-8")
    opened = target.stat()
    expected = f"{opened.st_dev}:{opened.st_ino}"
    hook = "rm() { return 99; }\n"

    result = subprocess.run(
        [
            "bash",
            "-c",
            hook + env_mod._DOCKER_REMOVE_OWNED_TEMP,
            "opencollab-remove",
            str(target),
            expected,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not target.exists()
    retired = list(tmp_path.glob(".opencollab-retired-*"))
    assert len(retired) == 1 and retired[0].read_text(encoding="utf-8") == "owned"


@pytest.mark.skipif(sys.platform != "linux", reason="Docker control script uses GNU mv")
def test_docker_retirement_destination_collision_preserves_foreign_inode(tmp_path):
    target = tmp_path / "owned.tmp"
    target.write_text("owned", encoding="utf-8")
    opened = target.stat()
    expected = f"{opened.st_dev}:{opened.st_ino}"
    victim = tmp_path / "foreign.txt"
    victim.write_text("foreign", encoding="utf-8")
    marker = tmp_path / "collision.injected"
    hook = f"""
mv() {{
    destination="${{@: -1}}"
    if [ ! -e {shlex.quote(str(marker))} ]; then
        : > {shlex.quote(str(marker))}
        ln {shlex.quote(str(victim))} "$destination"
    fi
    command mv "$@"
}}
"""

    result = subprocess.run(
        [
            "bash",
            "-c",
            hook + env_mod._DOCKER_REMOVE_OWNED_TEMP,
            "opencollab-remove",
            str(target),
            expected,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode in {0, 77}, result.stderr.decode(errors="replace")
    if result.returncode == 0:
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == "owned"
    foreign = victim.stat()
    assert foreign.st_nlink == 2
    retired = list(tmp_path.glob(".opencollab-retired-*"))
    assert any(entry.stat().st_ino == foreign.st_ino for entry in retired)
    if result.returncode == 0:
        assert any(entry.read_text(encoding="utf-8") == "owned" for entry in retired)


@pytest.mark.skipif(sys.platform != "linux", reason="Docker control script uses GNU mv")
def test_docker_retirement_lock_keeps_concurrent_count_at_cap(tmp_path):
    for index in range(255):
        (tmp_path / f".opencollab-retired-existing-{index}").touch()
    targets = [tmp_path / "first.tmp", tmp_path / "second.tmp"]
    expected = []
    for target in targets:
        target.write_text("owned", encoding="utf-8")
        opened = target.stat()
        expected.append(f"{opened.st_dev}:{opened.st_ino}")
    hook = "mv() { sleep 0.1; command mv \"$@\"; }\n"
    processes = [
        subprocess.Popen(
            [
                "bash",
                "-c",
                hook + env_mod._DOCKER_REMOVE_OWNED_TEMP,
                "opencollab-remove",
                str(target),
                identity,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for target, identity in zip(targets, expected, strict=True)
    ]

    results = [process.communicate(timeout=5) for process in processes]

    assert {process.returncode for process in processes} == {0, 78}, results
    assert len(list(tmp_path.glob(".opencollab-retired-*"))) == 256


def test_read_rejects_truncated_output(monkeypatch):
    monkeypatch.setattr(env_mod, "PROCESS_OUTPUT_CAPTURE_BYTES", 1024)
    fake = FakeDocker([FakeProc(stdout=b"x" * 2048)])
    patch_docker(monkeypatch, fake)
    with pytest.raises(OSError, match="exceeded capture limit"):
        run(DockerEnvironment(container_id="cid").read_file("big"))


def test_container_cancel_script_kills_real_inner_process_group(tmp_path):
    pidfile = tmp_path / "exec.pid"
    cancelfile = tmp_path / "exec.cancel"
    ready = tmp_path / "inner.ready"
    sentinel = tmp_path / "inner.sentinel"
    child_code = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
    wrapper = subprocess.Popen([
        "bash", "-c", env_mod._DOCKER_EXEC_WRAPPER, "wrapper-test",
        str(pidfile), str(cancelfile), "-c", command,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 1
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready.exists()

    cancel = subprocess.run([
        "bash", "-c", env_mod._DOCKER_EXEC_CANCEL, "cancel-test",
        str(pidfile), str(cancelfile),
    ], capture_output=True, timeout=3)
    wrapper.communicate(timeout=3)
    time.sleep(0.35)

    assert cancel.returncode == 0, cancel.stderr.decode(errors="replace")
    assert not sentinel.exists()


def test_container_cancel_marker_failure_and_missing_pid_are_nonzero(tmp_path):
    marker_is_directory = tmp_path / "marker-dir"
    marker_is_directory.mkdir()
    marker_failure = subprocess.run([
        "bash", "-c", env_mod._DOCKER_EXEC_CANCEL, "cancel-test",
        str(tmp_path / "missing.pid"), str(marker_is_directory),
    ], capture_output=True, timeout=3)
    assert marker_failure.returncode != 0

    missing_pid = subprocess.run([
        "bash", "-c", env_mod._DOCKER_EXEC_CANCEL, "cancel-test",
        str(tmp_path / "never.pid"), str(tmp_path / "created.cancel"),
    ], capture_output=True, timeout=3)
    assert missing_pid.returncode == 124


def test_container_wrapper_pidfile_failure_kills_child(tmp_path):
    pidfile_is_directory = tmp_path / "pid-dir"
    pidfile_is_directory.mkdir()
    sentinel = tmp_path / "pid-write-leak"
    child_code = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.4); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
    completed = subprocess.run([
        "bash", "-c", env_mod._DOCKER_EXEC_WRAPPER, "wrapper-test",
        str(pidfile_is_directory), str(tmp_path / "cancel"), "-c", command,
    ], capture_output=True, timeout=3)
    time.sleep(0.5)
    assert completed.returncode == 125
    assert not sentinel.exists()


def test_container_wrapper_preserves_binary_stdin(tmp_path):
    destination = tmp_path / "streamed.bin"
    content = b"abc\x00xyz" * 20_000
    completed = subprocess.run([
        "bash", "-c", env_mod._DOCKER_EXEC_WRAPPER, "write-wrapper-test",
        str(tmp_path / "write.pid"), str(tmp_path / "write.cancel"),
        "-c", 'cat > "$1"', "opencollab-write", str(destination),
    ], input=content, capture_output=True, timeout=3)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert destination.read_bytes() == content


def test_container_wrapper_cleans_group_after_command_leader_exits(tmp_path):
    sentinel = tmp_path / "late-background-write"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} &"

    completed = subprocess.run(
        [
            "bash",
            "-c",
            env_mod._DOCKER_EXEC_WRAPPER,
            "wrapper-test",
            str(tmp_path / "background.pid"),
            str(tmp_path / "background.cancel"),
            "-c",
            command,
        ],
        capture_output=True,
        timeout=4,
    )

    assert completed.returncode == 125, completed.stderr.decode(errors="replace")
    threading.Event().wait(0.9)
    assert not sentinel.exists()
