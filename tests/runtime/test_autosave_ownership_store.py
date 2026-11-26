from __future__ import annotations

import errno
import multiprocessing
import os
import signal
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from opencollab.adapters import storage
from opencollab.adapters.storage import SessionStore
from opencollab.application.ports import AutosaveOwnershipStorePort


def _hold_autosave_lock(path: str, ready: Event, stop: Event) -> None:
    release = SessionStore().try_autosave_lock(path)
    assert release is not None
    try:
        ready.set()
        assert stop.wait(timeout=15)
    finally:
        release()


def test_autosave_lock_coordinates_processes_and_recovers_after_kill(tmp_path: Path) -> None:
    path = str(tmp_path / "nested" / "session.json")
    lock_path = Path(f"{path}.autosave.lock")
    store = SessionStore()
    assert isinstance(store, AutosaveOwnershipStorePort)
    release = store.try_autosave_lock(path)
    assert release is not None
    release()
    inode = lock_path.stat().st_ino
    messages = [{"role": "user", "content": "saved"}]
    store.append_snapshot_delta(path, sequence=1, replace_from=0, messages=messages, meta={})

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop = context.Event()
    holder = context.Process(target=_hold_autosave_lock, args=(path, ready, stop))
    holder.start()
    try:
        assert ready.wait(timeout=10)
        assert store.try_autosave_lock(path) is None
        assert store.load_snapshot(path, "system")["messages"] == messages
    finally:
        holder.kill()
        holder.join(timeout=10)
    assert holder.exitcode == -signal.SIGKILL

    release = store.try_autosave_lock(path)
    assert release is not None
    release()
    assert lock_path.stat().st_ino == inode


def test_autosave_lock_propagates_io_failure_and_closes_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "session.json")
    Path(f"{path}.autosave.lock").touch()
    opened: list[int] = []

    def fail_lock(fd: int, operation: int) -> None:
        assert operation == storage.fcntl.LOCK_EX | storage.fcntl.LOCK_NB
        opened.append(fd)
        raise OSError(errno.EIO, "flock failed")

    monkeypatch.setattr(storage.fcntl, "flock", fail_lock)
    with pytest.raises(OSError) as error:
        SessionStore().try_autosave_lock(path)
    assert error.value.errno == errno.EIO

    (fd,) = opened
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF


def test_autosave_lock_rejects_dangling_sidecar_symlink(tmp_path: Path) -> None:
    path = str(tmp_path / "session.json")
    target = tmp_path / "missing"
    Path(f"{path}.autosave.lock").symlink_to(target)

    with pytest.raises(OSError):
        SessionStore().try_autosave_lock(path)
    assert not target.exists()
