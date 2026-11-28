from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from opencollab.adapters import candidate_workspace, vm_isolation, worktree_pool
from opencollab.adapters._env_vm import VMEnvironment, VMOperationError, VMTransportError
from opencollab.adapters._vm_leases import VMCapacityError, _VMLeases
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.adapters.vm_config import VMConnection, VMResource
from opencollab.adapters.vm_isolation import VMIsolationPool
from opencollab.bootstrap.vm import vm_isolation_provider


def _repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "answer.txt").write_text("initial\n")
    for arguments in (("add", "."), ("-c", "user.name=Tester", "-c", "user.email=test@example.com",
                                         "commit", "--quiet", "-m", "initial")):
        subprocess.run(["git", "-C", str(path), *arguments], check=True)


def _resource(path: Path, resource_id: str = "vm-one", *, reset_command: str = "true") -> VMResource:
    path.mkdir(exist_ok=True)
    return VMResource(resource_id, VMConnection("localhost", "test"), str(path), str(path.parent / "state"),
                      reset_command)


class _LocalVM(VMEnvironment):
    copy_failure = False

    def __init__(self, connection, workspace, *, state_directory, run_id=None):
        super().__init__(connection, workspace, state_directory=state_directory, run_id=run_id)
        self.local = LocalEnvironment(workspace)

    async def setup(self, mount_dir=None):
        self._ensure_active()
        return self.workspace

    async def exec_cmd(self, cmd, timeout=120, *, input_bytes=None):
        self._ensure_active()
        return await self.local.exec_cmd(cmd, timeout, input_bytes=input_bytes)

    async def exec_operation(self, operation_id, cmd, timeout=120, *, input_bytes=None):
        return await self.exec_cmd(cmd, timeout, input_bytes=input_bytes)

    async def write_temp_file(self, *args, **kwargs):
        return await self.local.write_temp_file(*args, **kwargs)

    async def remove_file(self, path):
        await self.local.remove_file(path)

    @asynccontextmanager
    async def _mutable_temporary(self, content, *, prefix, suffix):
        with tempfile.TemporaryDirectory(prefix="opencollab-vm-unit-") as directory:
            path = Path(directory) / (prefix + suffix)
            path.write_text(content)
            yield str(path)

    async def copy_to_host(self, source, destination, *, directory=False):
        if self.copy_failure:
            raise OSError("transfer interrupted")
        if directory:
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, destination)

    async def copy_from_host(self, source, destination, *, directory=False):
        if directory:
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, destination)

    async def cleanup(self):
        if self._uncertain:
            raise VMOperationError("operation status unknown", operation_id="unknown", state="unknown")
        self.revoke()
        await self.local.cleanup()


@pytest.fixture
def local_vm(monkeypatch):
    monkeypatch.setattr(vm_isolation, "VMEnvironment", _LocalVM)
    monkeypatch.setattr(candidate_workspace, "VMEnvironment", _LocalVM)
    monkeypatch.setattr(worktree_pool, "VMEnvironment", _LocalVM)
    monkeypatch.setattr(_LocalVM, "copy_failure", False)


async def test_database_claim_is_exclusive_across_controller_processes_and_connection_aliases(tmp_path):
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    leases = _VMLeases(database, [resource], 0)
    await leases.acquire("parent")
    code = """
import asyncio, sys
from opencollab.adapters._vm_leases import _VMLeases, VMCapacityError
from opencollab.adapters.vm_config import VMConnection, VMResource
resource = VMResource('vm-one', VMConnection('different-alias', 'test'), sys.argv[2], reset_command='true')
async def main():
    try:
        await _VMLeases(sys.argv[1], [resource], 0).acquire('child')
    except VMCapacityError:
        print('exhausted')
    else:
        print('claimed')
asyncio.run(main())
"""
    def child():
        return subprocess.check_output(
            [sys.executable, "-c", code, database, resource.workspace_root], text=True,
        ).strip()
    assert await asyncio.to_thread(child) == "exhausted"
    leases.release(resource, "parent")
    assert await asyncio.to_thread(child) == "claimed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT resource_id, owner FROM vm_leases").fetchall() == [("vm-one", "child")]


async def test_resource_waiting_is_cancellable_without_claiming_the_occupied_vm(tmp_path):
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    immediate = _VMLeases(database, [resource], 0)
    await immediate.acquire("parent")
    waiting = _VMLeases(database, [resource], None)
    task = asyncio.create_task(waiting.acquire("cancelled"))
    await asyncio.sleep(0.08)
    task.cancel("stop nested run")
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    # asyncio forwards the cancellation message to awaiters from Python 3.11.
    if sys.version_info >= (3, 11):
        assert str(cancelled.value) == "stop nested run"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT owner FROM vm_leases").fetchall() == [("parent",)]
    immediate.release(resource, "parent")
    assert await waiting.acquire("next") == resource


async def test_group_sessions_are_separate_and_exports_keep_binary_symlinks_and_history(
    tmp_path, local_vm, monkeypatch,
):
    source = tmp_path / "source"
    _repository(source)
    (source / "payload.bin").write_bytes(b"\0\xff\x80")
    (source / "shortcut").symlink_to("answer.txt")
    (source / "executable").write_text("#!/bin/sh\nexit 0\n")
    (source / "executable").chmod(0o755)
    pool = VMIsolationPool(str(source), resources=[_resource(tmp_path / "guest")],
                           lease_database=str(tmp_path / "leases.sqlite"), owned_save_dir=str(tmp_path / "results"))
    first, second = await pool.acquire("first"), await pool.acquire("second")
    assert first.resource_id == second.resource_id == "vm-one"
    assert first.workspace != second.workspace
    await first.exec_cmd("printf 'changed\\n' > answer.txt && printf 'log\\n' > run.log")
    assert (await second.exec_cmd("cat answer.txt")).stdout == "initial\n"
    destination = Path(first.export_directory)
    replace = os.replace

    def require_same_filesystem_staging(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "cross-device link")
        replace(source, target)

    monkeypatch.setattr(vm_isolation.os, "replace", require_same_filesystem_staging)
    await pool.release()
    assert not pool.retained_resources
    assert (destination / "payload.bin").read_bytes() == b"\0\xff\x80"
    assert (destination / "shortcut").is_symlink()
    assert os.readlink(destination / "shortcut") == "answer.txt"
    assert (destination / "executable").stat().st_mode & 0o777 == 0o755
    assert (destination / "run.log").read_text() == "log\n"
    assert subprocess.check_output(["git", "-C", str(destination), "show", "HEAD:answer.txt"], text=True) == "initial\n"
    assert (source / "answer.txt").read_text() == "initial\n"


async def test_session_scopes_claim_different_vms_and_nested_exhaustion_is_clear(tmp_path, local_vm):
    source = tmp_path / "source"
    _repository(source)
    resources = [_resource(tmp_path / "guest-one", "one"), _resource(tmp_path / "guest-two", "two")]
    provider = vm_isolation_provider(resources, lease_database=str(tmp_path / "leases.sqlite"), scope="session")
    sessions = SessionEnvironmentPool(str(source), isolation="vm-session", providers={"vm-session": provider})
    first, second = await sessions.acquire("first"), await sessions.acquire("second")
    assert first.resource_id != second.resource_id
    child = sessions.child_pool(first, role="child")
    with pytest.raises(VMCapacityError, match="capacity exhausted"):
        await child.acquire("nested")
    await child.release()
    await sessions.release()


async def test_export_failure_retains_workspace_and_lease_until_retry(tmp_path, local_vm, monkeypatch):
    source = tmp_path / "source"
    _repository(source)
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    pool = VMIsolationPool(str(source), resources=[resource], lease_database=database)
    environment = await pool.acquire("worker")
    await environment.exec_cmd("printf 'candidate\\n' > answer.txt")
    monkeypatch.setattr(_LocalVM, "copy_failure", True)
    with pytest.raises(OSError, match="retained"):
        await pool.release()
    assert Path(environment.workspace).is_dir()
    assert pool.retained_resources == (resource.resource_id,)
    with pytest.raises(VMCapacityError):
        await _VMLeases(database, [resource], 0).acquire("other")
    monkeypatch.setattr(_LocalVM, "copy_failure", False)
    await pool.release()
    assert (Path(environment.export_directory) / "answer.txt").read_text() == "candidate\n"
    assert not Path(environment.workspace).exists()


@pytest.mark.parametrize("stage", ["capture", "cleanup"])
async def test_lost_candidate_reply_recovers_before_export_or_cleanup_retry(tmp_path, local_vm, monkeypatch, stage):
    source = tmp_path / "source"
    _repository(source)
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    pool = VMIsolationPool(str(source), resources=[resource], lease_database=database, scope="session")
    environment = await pool.acquire("worker")
    await environment.exec_cmd("printf candidate > answer.txt")
    control = environment._lease.base_environment
    execute = control.exec_cmd
    completed = {}
    recovered = []
    available = False

    async def lose_reply(cmd, timeout=120, *, input_bytes=None):
        result = await execute(cmd, timeout, input_bytes=input_bytes)
        selected = environment._delegate.revoked if stage == "capture" else "worktree list --porcelain" in cmd
        if selected and not completed:
            completed["candidate-reply"] = result
            control._operations.add("candidate-reply")
            control._uncertain.add("candidate-reply")
            raise VMTransportError("candidate reply lost", operation_id="candidate-reply")
        return result

    async def recover(operation_id):
        if not available:
            raise VMOperationError("operation status unknown", operation_id=operation_id, state="unknown")
        recovered.append(operation_id)
        control._operations.remove(operation_id)
        control._uncertain.remove(operation_id)
        return completed[operation_id]

    monkeypatch.setattr(control, "exec_cmd", lose_reply)
    monkeypatch.setattr(control, "recover_operation", recover)
    destination = Path(environment.export_directory)
    with pytest.raises(VMTransportError):
        await pool.release_env(environment)
    assert destination.exists() is (stage == "cleanup")
    with pytest.raises(VMOperationError):
        await pool.release_env(environment)
    with pytest.raises(VMCapacityError):
        await _VMLeases(database, [resource], 0).acquire("other")
    assert Path(environment.workspace).exists()
    available = True
    await pool.release_env(environment)
    assert recovered == ["candidate-reply"]
    assert (destination / "answer.txt").read_text() == "candidate"
    assert not pool.retained_resources
    assert await _VMLeases(database, [resource], 0).acquire("next") == resource
    await pool.release()


async def test_reset_failure_retains_lease_and_restores_guest_before_next_claim(tmp_path, local_vm):
    source = tmp_path / "source"
    _repository(source)
    marker, ready = tmp_path / "global-state", tmp_path / "reset-ready"
    marker.write_text("dirty")
    resource = _resource(tmp_path / "guest", reset_command=f"test -f '{ready}' && rm -f '{marker}'")
    database = str(tmp_path / "leases.sqlite")
    pool = VMIsolationPool(str(source), resources=[resource], lease_database=database)
    await pool.acquire("worker")
    with pytest.raises(OSError, match="retained"):
        await pool.release()
    assert marker.exists()
    with pytest.raises(VMCapacityError):
        await _VMLeases(database, [resource], 0).acquire("other")
    ready.touch()
    await pool.release()
    assert not marker.exists()
    assert await _VMLeases(database, [resource], 0).acquire("next") == resource


async def test_scoped_session_release_retries_failed_reset_and_returns_capacity(tmp_path, local_vm):
    source = tmp_path / "source"
    _repository(source)
    marker, ready = tmp_path / "guest-marker", tmp_path / "reset-ready"
    marker.write_text("dirty")
    resource = _resource(tmp_path / "guest", reset_command=f"test -f '{ready}' && rm -f '{marker}'")
    database = str(tmp_path / "leases.sqlite")
    provider = vm_isolation_provider([resource], lease_database=database, scope="session")
    pool = SessionEnvironmentPool(
        str(source), isolation="vm-session", providers={"vm-session": provider},
        owned_save_dir=str(tmp_path / "results"),
    )

    def lease_count():
        with sqlite3.connect(database) as connection:
            return connection.execute("SELECT count(*) FROM vm_leases").fetchone()[0]

    try:
        environment = await pool.acquire("worker")
        await environment.exec_cmd("printf recorded > answer.txt")
        export = Path(environment.export_directory)
        with pytest.raises(RuntimeError, match="VM guest reset"):
            await pool.release_env(environment)
        assert marker.exists() and lease_count() == 1
        with pytest.raises(VMCapacityError, match="capacity exhausted"):
            await pool.acquire("too-early")

        ready.touch()
        await pool.release_env(environment)
        assert not marker.exists() and lease_count() == 0
        replacement = await pool.acquire("next-role")
        assert replacement.resource_id == resource.resource_id
        await pool.release_env(replacement)
        assert lease_count() == 0
        assert (export / "answer.txt").read_text() == "recorded"
    finally:
        ready.touch()
        await pool.release()


async def test_failed_vm_reset_still_returns_an_independent_guest(tmp_path, local_vm):
    source = tmp_path / "source"
    _repository(source)
    ready = tmp_path / "reset-ready"
    resources = [
        _resource(tmp_path / "healthy", "healthy"),
        _resource(tmp_path / "recovering", "recovering", reset_command=f"test -f '{ready}'"),
    ]
    database = str(tmp_path / "leases.sqlite")
    provider = vm_isolation_provider(resources, lease_database=database, scope="session")
    pool = SessionEnvironmentPool(
        str(source), isolation="vm-session", providers={"vm-session": provider},
        owned_save_dir=str(tmp_path / "results"),
    )
    registry = _VMLeases(database, resources, 0)
    try:
        first = await pool.acquire("report-first")
        second = await pool.acquire("report-second")
        assert (await first.exec_cmd("printf first > answer.txt")).returncode == 0
        assert (await second.exec_cmd("printf second > answer.txt")).returncode == 0

        with pytest.raises(RuntimeError, match="VM guest reset"):
            await pool.release()
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT resource_id FROM vm_leases").fetchall() == [("recovering",)]
        for environment, text in ((first, "first"), (second, "second")):
            assert (Path(environment.export_directory) / "answer.txt").read_text() == text
        replacement = await registry.acquire("new-run")
        assert replacement == resources[0]
        registry.release(replacement, "new-run")
    finally:
        ready.touch()
        await pool.release()
    with sqlite3.connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM vm_leases").fetchall()


async def test_release_interrupts_configured_capacity_wait(tmp_path, local_vm):
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    registry = _VMLeases(database, [resource], 0)
    await registry.acquire("outer")
    pool = VMIsolationPool(str(tmp_path), resources=[resource], lease_database=database, wait_timeout=None)
    acquisition = asyncio.create_task(pool.acquire("nested"))
    await asyncio.sleep(0.08)
    await asyncio.wait_for(pool.release(), 1)
    with pytest.raises(RuntimeError, match="closed"):
        await acquisition
    registry.release(resource, "outer")


@pytest.mark.parametrize("owns_vm", [False, True], ids=["another-controller", "same-pool"])
async def test_session_pool_release_interrupts_capacity_wait(tmp_path, local_vm, monkeypatch, owns_vm):
    source = tmp_path / "source"
    _repository(source)
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    registry = _VMLeases(database, [resource], 0)
    provider = vm_isolation_provider([resource], lease_database=database, scope="session", wait_timeout=None)
    pool = SessionEnvironmentPool(
        str(source), isolation="vm-session", providers={"vm-session": provider},
        owned_save_dir=str(tmp_path / "results"),
    )
    if owns_vm:
        active = await pool.acquire("first")
        await active.exec_cmd("printf recorded > answer.txt")
    else:
        await registry.acquire("other-controller")

    attempted = asyncio.Event()
    real_acquire = _VMLeases._try_acquire

    def observe_acquire(leases, owner):
        result = real_acquire(leases, owner)
        assert result is None
        attempted.set()
        return result

    monkeypatch.setattr(_VMLeases, "_try_acquire", observe_acquire)
    waiting = asyncio.create_task(pool.acquire("waiting"))
    closing = None
    try:
        await asyncio.wait_for(attempted.wait(), 5)
        closing = asyncio.create_task(pool.release())
        done, _ = await asyncio.wait({closing}, timeout=5)
        assert closing in done
        await closing
        with pytest.raises(RuntimeError, match="closed"):
            await waiting
        expected = [] if owns_vm else [("other-controller",)]
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT owner FROM vm_leases").fetchall() == expected
        if owns_vm:
            assert (Path(active.export_directory) / "answer.txt").read_text() == "recorded"
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        if closing is not None:
            await closing
        else:
            await pool.release()
        if not owns_vm:
            registry.release(resource, "other-controller")


async def test_sessions_prepare_independently_and_release_capacity_to_waiters(tmp_path, local_vm, monkeypatch):
    source = tmp_path / "source"
    _repository(source)
    resources = [_resource(tmp_path / "guest-one", "one"), _resource(tmp_path / "guest-two", "two")]
    pool = VMIsolationPool(str(source), resources=resources, lease_database=str(tmp_path / "leases.sqlite"),
                           scope="session", wait_timeout=None)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = pool._prepare_owner
    preparing = 0

    async def prepare(owner, role, parent):
        nonlocal preparing
        preparing += 1
        if preparing == 2:
            entered.set()
        await proceed.wait()
        await original(owner, role, parent)

    monkeypatch.setattr(pool, "_prepare_owner", prepare)
    tasks = [asyncio.create_task(pool.acquire(role)) for role in ("first", "second")]
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        proceed.set()
    first, second = await asyncio.gather(*tasks)
    waiter = asyncio.create_task(pool.acquire("third"))
    await asyncio.sleep(0.08)
    assert not waiter.done()
    await pool.release_env(first)
    third = await asyncio.wait_for(waiter, 10)
    assert third.resource_id == first.resource_id
    assert second.resource_id != third.resource_id
    await pool.release()


async def test_unknown_execution_retains_the_vm_until_recovery(tmp_path, local_vm):
    source = tmp_path / "source"
    _repository(source)
    resource = _resource(tmp_path / "guest")
    database = str(tmp_path / "leases.sqlite")
    pool = VMIsolationPool(str(source), resources=[resource], lease_database=database)
    environment = await pool.acquire("worker")
    environment._delegate._uncertain.add("unknown")
    with pytest.raises(OSError, match="retained"):
        await pool.release()
    assert Path(environment.workspace).exists()
    with pytest.raises(VMCapacityError):
        await _VMLeases(database, [resource], 0).acquire("other")
    environment._delegate._uncertain.clear()
    await pool.release()


async def test_lost_reset_reply_recovers_the_same_operation_without_repeating_reset(tmp_path, local_vm, monkeypatch):
    source = tmp_path / "source"
    _repository(source)
    counter = tmp_path / "reset-count"
    command = f"printf x >> '{counter}'"
    resource = _resource(tmp_path / "guest", reset_command=command)
    pool = VMIsolationPool(str(source), resources=[resource], lease_database=str(tmp_path / "leases.sqlite"))
    await pool.acquire("worker")
    original = _LocalVM.exec_operation
    completed = {}

    async def lose_reply(self, operation_id, cmd, timeout=120, *, input_bytes=None):
        result = await original(self, operation_id, cmd, timeout, input_bytes=input_bytes)
        if cmd == command:
            completed[operation_id] = result
            self._operations.add(operation_id)
            self._uncertain.add(operation_id)
            raise VMTransportError("reset response lost", operation_id=operation_id)
        return result

    async def recover(self, operation_id):
        self._operations.remove(operation_id)
        self._uncertain.remove(operation_id)
        return completed[operation_id]

    monkeypatch.setattr(_LocalVM, "exec_operation", lose_reply)
    monkeypatch.setattr(_LocalVM, "recover_operation", recover)
    with pytest.raises(OSError, match="retained"):
        await pool.release()
    assert counter.read_text() == "x"
    await pool.release()
    assert counter.read_text() == "x"
    assert not pool.retained_resources


def test_reusable_pool_requires_explicit_guest_reset(tmp_path):
    resource = VMResource("one", VMConnection("localhost", "test"))
    with pytest.raises(ValueError, match="reset_command"):
        VMIsolationPool(str(tmp_path), resources=[resource], lease_database=str(tmp_path / "leases.sqlite"))
