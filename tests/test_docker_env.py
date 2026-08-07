"""Black-box ownership and command tests for DockerEnvironment."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_local import LOCAL_FILE_WRITE_LIMIT_BYTES
from opencollab.adapters._env_process import ProcessCleanupError, ProcessResult
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment

CONTAINER_ID = "a" * 64
OTHER_ID = "b" * 64


def _result(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    *,
    stdout_dropped: int = 0,
    stderr_dropped: int = 0,
) -> ProcessResult:
    return ProcessResult(
        returncode,
        stdout,
        stderr,
        stdout_dropped_bytes=stdout_dropped,
        stderr_dropped_bytes=stderr_dropped,
    )


class FakeDocker:
    def __init__(self, handler: Callable | None = None) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    async def __call__(self, command, **kwargs):
        command = tuple(command)
        self.calls.append((command, kwargs))
        if self.handler is None:
            return _result()
        value = self.handler(command, kwargs)
        if isinstance(value, BaseException):
            raise value
        return value


def _patch(monkeypatch, fake: FakeDocker) -> None:
    monkeypatch.setattr(docker_module, "run_process", fake)


@pytest.mark.parametrize(
    ("probe_result", "expected_returncode", "pidfile_retained"),
    [(0, 125, True), (1, 0, False)],
    ids=("group-survives", "group-quiesces"),
)
def test_exec_cancel_only_succeeds_after_process_group_quiesces(
    tmp_path,
    probe_result,
    expected_returncode,
    pidfile_retained,
) -> None:
    pidfile = tmp_path / "exec.pid"
    pidfile.write_text("123\n", encoding="utf-8")
    command = (
        f'kill() {{ [ "$1" = "-0" ] && return {probe_result}; return 0; }}; '
        "sleep() { :; }; "
        f"{docker_module._EXEC_CANCEL}"
    )

    result = subprocess.run(
        ["bash", "-c", command, "opencollab-cancel", str(pidfile)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_returncode
    assert pidfile.exists() is pidfile_retained


async def test_owned_setup_is_network_isolated_and_cleanup_uses_full_id(monkeypatch) -> None:
    def respond(command, _kwargs):
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1:4] == ("rm", "-f", "--"):
            return _result()
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment(image="python:3.11")
    assert await env.setup() == CONTAINER_ID
    run_command = fake.calls[0][0]
    assert run_command[:2] == ("docker", "run")
    assert run_command[run_command.index("--network") + 1] == "none"
    assert "opencollab.owner=" in " ".join(run_command)
    await env.cleanup()
    assert fake.calls[-1][0] == ("docker", "rm", "-f", "--", CONTAINER_ID)


async def test_start_failure_never_removes_foreign_name_collision(monkeypatch) -> None:
    def respond(command, _kwargs):
        if command[1] == "run":
            return _result(returncode=125, stderr=b"name conflict")
        if command[1] == "inspect":
            return _result(stdout=f"{OTHER_ID}\tforeign-owner\n".encode())
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Failed to start"):
        await DockerEnvironment().setup()
    assert all(call[0][1] != "rm" for call in fake.calls)


async def test_start_failure_reports_unproven_inspect_cleanup(monkeypatch) -> None:
    def respond(command, _kwargs):
        if command[1] == "run":
            return asyncio.TimeoutError()
        if command[1] == "inspect":
            return _result(returncode=1, stderr=b"daemon unavailable")
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    with pytest.raises(ProcessCleanupError, match="removal was not proven"):
        await DockerEnvironment().setup()
    assert all(call[0][1] != "rm" for call in fake.calls)


async def test_attached_name_binds_once_and_executes_by_full_id(monkeypatch) -> None:
    def respond(command, _kwargs):
        if command[1] == "inspect":
            return _result(stdout=f"{CONTAINER_ID}\t/swe-task\ttrue\n".encode())
        if command[1] == "exec":
            return _result(stdout=b"done")
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment(container_id="swe-task", workspace="/repo")
    assert await env.setup() == CONTAINER_ID
    result = await env.exec_cmd("git status")
    assert result.stdout == "done"
    exec_command = fake.calls[-1][0]
    assert CONTAINER_ID in exec_command
    assert "swe-task" not in exec_command
    await env.cleanup()
    assert all(call[0][1] != "rm" for call in fake.calls)


async def test_attached_binding_ignores_nonpositive_dropped_output_metadata(monkeypatch) -> None:
    fake = FakeDocker(
        lambda command, _kwargs: _result(
            stdout=f"{CONTAINER_ID}\t/swe-task\ttrue\n".encode(),
            stdout_dropped=-1,
        )
    )
    _patch(monkeypatch, fake)

    env = DockerEnvironment(container_id="swe-task")

    assert await env.setup() == CONTAINER_ID


async def test_attached_binding_rejects_identity_mismatch(monkeypatch) -> None:
    fake = FakeDocker(
        lambda command, _kwargs: _result(stdout=f"{CONTAINER_ID}\t/other\ttrue\n".encode())
    )
    _patch(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="ambiguous or changed"):
        await DockerEnvironment(container_id="expected").setup()


async def test_attached_reference_validation_rejects_options_and_short_ids() -> None:
    for value in ("--privileged", "abc123", "bad/name"):
        with pytest.raises(ValueError, match="unsafe or ambiguous"):
            DockerEnvironment(container_id=value)


async def test_exec_preserves_bounded_output_metadata(monkeypatch) -> None:
    def respond(command, _kwargs):
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "exec":
            return _result(stdout=b"kept", stderr=b"error", stdout_dropped=12, stderr_dropped=3)
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    result = await env.exec_cmd("emit")
    assert result.stdout == "kept"
    assert result.stderr == "error"
    assert result.stdout_truncated and result.stderr_truncated
    assert result.stdout_dropped_bytes == 12
    assert result.stderr_dropped_bytes == 3


async def test_user_exit_125_does_not_destroy_owned_container(monkeypatch) -> None:
    exec_attempts = 0

    def respond(command, _kwargs):
        nonlocal exec_attempts
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "exec":
            exec_attempts += 1
            return _result(returncode=125 if exec_attempts == 1 else 0, stdout=b"usable")
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    assert (await env.exec_cmd("exit 125")).returncode == 125
    assert (await env.exec_cmd("printf usable")).stdout == "usable"
    assert not env.revoked
    assert all(call[0][1] != "rm" for call in fake.calls)


async def test_timeout_runs_container_inner_cancel_before_return(monkeypatch) -> None:
    exec_attempts = 0

    def respond(command, _kwargs):
        nonlocal exec_attempts
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "exec":
            exec_attempts += 1
            if exec_attempts == 1:
                return asyncio.TimeoutError()
            return _result()
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    result = await env.exec_cmd("sleep 20", timeout=0.01)
    assert result.returncode == -1
    assert "timed out" in result.stderr
    assert exec_attempts == 2
    assert docker_module._EXEC_CANCEL in fake.calls[-1][0]


async def test_attached_timeout_revokes_when_inner_cancel_fails(monkeypatch) -> None:
    exec_attempts = 0

    def respond(command, _kwargs):
        nonlocal exec_attempts
        if command[1] == "inspect":
            return _result(stdout=f"{CONTAINER_ID}\t/swe-task\ttrue\n".encode())
        if command[1] == "exec":
            exec_attempts += 1
            if exec_attempts == 1:
                return asyncio.TimeoutError()
            return _result(returncode=124)
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment(container_id="swe-task")
    await env.setup()
    with pytest.raises(ProcessCleanupError, match="did not quiesce"):
        await env.exec_cmd("sleep 20", timeout=0.01)
    assert env.revoked
    assert all(call[0][1] != "rm" for call in fake.calls)


async def test_attached_abort_cancels_and_waits_for_all_active_execs(monkeypatch) -> None:
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    cancellations: list[tuple[str, ...]] = []
    command_count = 0

    async def fake_docker(*args, **_kwargs):
        nonlocal command_count
        if docker_module._EXEC_WRAPPER in args:
            index = command_count
            command_count += 1
            started[index].set()
            await release.wait()
            return _result()
        if docker_module._EXEC_CANCEL in args:
            cancellations.append(args)
            if len(cancellations) == 2:
                release.set()
            return _result()
        raise AssertionError(args)

    env = DockerEnvironment(container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_docker", fake_docker)
    first = asyncio.create_task(env.exec_cmd("sleep 20"))
    second = asyncio.create_task(env.exec_cmd("sleep 20"))
    await asyncio.gather(*(event.wait() for event in started))

    await env.abort()

    assert env.revoked
    assert len(cancellations) == 2
    assert first.done() and second.done()
    await asyncio.gather(first, second)
    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("echo after-abort")


async def test_double_cancellation_cannot_interrupt_container_recovery(monkeypatch) -> None:
    fake = FakeDocker(
        lambda command, _kwargs: _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "run"
        else AssertionError(command)
    )
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    command_started = asyncio.Event()
    recovery_started = asyncio.Event()
    recovery_release = asyncio.Event()
    recovery_done = asyncio.Event()

    async def blocked_command(*_args, **_kwargs):
        command_started.set()
        await asyncio.Event().wait()

    async def recover(_token):
        recovery_started.set()
        await recovery_release.wait()
        recovery_done.set()
        return True

    monkeypatch.setattr(env, "_docker", blocked_command)
    monkeypatch.setattr(env, "_recover_inner", recover)
    owner = asyncio.create_task(env.exec_cmd("sleep 20"))
    await command_started.wait()
    owner.cancel()
    await recovery_started.wait()
    owner.cancel()
    recovery_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert recovery_done.is_set()


async def test_double_cancellation_finishes_container_and_backing_cleanup(monkeypatch) -> None:
    backing_cleaned = asyncio.Event()

    class BackingEnvironment:
        source_workspace = "/source"

        async def cleanup(self):
            backing_cleaned.set()

    fake = FakeDocker(
        lambda command, _kwargs: _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "run"
        else AssertionError(command)
    )
    _patch(monkeypatch, fake)
    env = DockerEnvironment(backing_environment=BackingEnvironment())
    await env.setup()
    removal_started = asyncio.Event()
    removal_release = asyncio.Event()
    removal_done = asyncio.Event()

    async def remove_container():
        removal_started.set()
        await removal_release.wait()
        removal_done.set()
        return True

    monkeypatch.setattr(env, "_remove_container_if_owned", remove_container)
    owner = asyncio.create_task(env.cleanup())
    await removal_started.wait()
    owner.cancel()
    owner.cancel()
    removal_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert removal_done.is_set()
    assert backing_cleaned.is_set()


async def test_verified_write_threads_stdin_and_digest(monkeypatch) -> None:
    payload = b"hello"
    digest = __import__("hashlib").sha256(payload).hexdigest()

    def respond(command, kwargs):
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        if command[1] == "exec":
            assert kwargs["input_bytes"] == payload
            return _result(stdout=f"5\t{digest}\n".encode())
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    await env.write_file("/workspace/result.txt", "hello")


async def test_write_half_input_failure_keeps_old_target_and_cleans_temp(monkeypatch) -> None:
    old = b"old content"
    files = {"/repo/result.txt": old}
    partial_temp: list[bytes] = []
    cleaned: list[str] = []

    async def fake_docker(*args, input_bytes=None, **_kwargs):
        if docker_module._EXEC_WRAPPER in args:
            assert input_bytes is not None
            if 'cat > "$target"' in args[-1]:
                files["/repo/result.txt"] = input_bytes[:2]
            else:
                partial_temp.append(input_bytes[:2])
                assert 'cat > "$temporary"' in args[-1]
                assert 'mv -f -- "$temporary" "$target"' in args[-1]
            return _result(returncode=1, stderr=b"stdin closed")
        if args[:2] == ("exec", "--"):
            cleaned.append(args[-1])
            return _result()
        raise AssertionError(args)

    env = DockerEnvironment(container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_docker", fake_docker)

    with pytest.raises(OSError, match="verification failed"):
        await env.write_file("/repo/result.txt", "new content")

    assert files["/repo/result.txt"] == old
    assert partial_temp == [b"ne"]
    assert len(cleaned) == 1
    assert ".opencollab-write-" in cleaned[0]


async def test_write_timeout_keeps_old_target_and_cleans_temp(monkeypatch) -> None:
    old = b"old content"
    files = {"/repo/result.txt": old}
    cleaned: list[str] = []

    async def fake_docker(*args, input_bytes=None, **_kwargs):
        if docker_module._EXEC_WRAPPER in args:
            assert input_bytes is not None
            if 'cat > "$target"' in args[-1]:
                files["/repo/result.txt"] = input_bytes[:2]
            raise asyncio.TimeoutError()
        if docker_module._EXEC_CANCEL in args:
            return _result()
        if args[:2] == ("exec", "--"):
            cleaned.append(args[-1])
            return _result()
        raise AssertionError(args)

    env = DockerEnvironment(container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_docker", fake_docker)

    with pytest.raises(OSError, match="verification failed"):
        await env.write_file("/repo/result.txt", "new content")

    assert files["/repo/result.txt"] == old
    assert len(cleaned) == 1
    assert ".opencollab-write-" in cleaned[0]


async def test_cancelled_write_keeps_old_target_and_cleans_temp(monkeypatch) -> None:
    old = b"old content"
    files = {"/repo/result.txt": old}
    started = asyncio.Event()
    cleaned: list[str] = []

    async def fake_docker(*args, input_bytes=None, **_kwargs):
        if docker_module._EXEC_WRAPPER in args:
            assert input_bytes is not None
            if 'cat > "$target"' in args[-1]:
                files["/repo/result.txt"] = input_bytes[:2]
            started.set()
            await asyncio.Event().wait()
        if docker_module._EXEC_CANCEL in args:
            return _result()
        if args[:2] == ("exec", "--"):
            cleaned.append(args[-1])
            return _result()
        raise AssertionError(args)

    env = DockerEnvironment(container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_docker", fake_docker)
    owner = asyncio.create_task(env.write_file("/repo/result.txt", "new content"))
    await started.wait()
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner

    assert files["/repo/result.txt"] == old
    assert len(cleaned) == 1
    assert ".opencollab-write-" in cleaned[0]


async def test_concurrent_same_container_path_writes_are_serialized(monkeypatch) -> None:
    target = b"old"
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_docker(*args, input_bytes=None, **_kwargs):
        nonlocal target, active, max_active
        if docker_module._EXEC_WRAPPER not in args:
            raise AssertionError(args)
        assert input_bytes is not None
        active += 1
        max_active = max(max_active, active)
        if not first_started.is_set():
            first_started.set()
            await release_first.wait()
        target = input_bytes
        active -= 1
        digest = __import__("hashlib").sha256(input_bytes).hexdigest()
        return _result(stdout=f"{len(input_bytes)}\t{digest}\n".encode())

    first = DockerEnvironment(container_id=CONTAINER_ID)
    second = DockerEnvironment(container_id=CONTAINER_ID)
    first._attached_bound = True
    second._attached_bound = True
    monkeypatch.setattr(first, "_docker", fake_docker)
    monkeypatch.setattr(second, "_docker", fake_docker)
    first_write = asyncio.create_task(first.write_file("/repo/result.txt", "A" * 2000))
    await first_started.wait()
    second_write = asyncio.create_task(second.write_file("/repo/./result.txt", "B" * 2000))
    await asyncio.sleep(0)
    assert max_active == 1
    release_first.set()
    await asyncio.gather(first_write, second_write)

    assert max_active == 1
    assert target in {b"A" * 2000, b"B" * 2000}


@pytest.mark.parametrize("temporary", [False, True])
async def test_docker_writes_reject_oversize_utf8_before_transport(
    monkeypatch,
    temporary,
) -> None:
    calls = 0

    async def fake_docker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result()

    env = DockerEnvironment(container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_docker", fake_docker)
    content = "é" * (LOCAL_FILE_WRITE_LIMIT_BYTES // 2 + 1)

    with pytest.raises(OSError, match="write limit"):
        if temporary:
            await env.write_temp_file(content, prefix="probe-")
        else:
            await env.write_file("/repo/result.txt", content)

    assert calls == 0


async def test_only_docker_declares_os_process_isolation(tmp_path) -> None:
    assert DockerEnvironment().process_isolated
    assert not LocalEnvironment(str(tmp_path)).process_isolated
