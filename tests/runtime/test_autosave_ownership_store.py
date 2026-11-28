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
from opencollab.application.ports import AutosaveOwnershipStorePort, SnapshotSequenceConflict


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
        assert store.try_autosave_lock(path, expected_sequence=0) is None
        assert store.load_snapshot(path, "system")["messages"] == messages
    finally:
        holder.kill()
        holder.join(timeout=10)
    assert holder.exitcode == -signal.SIGKILL

    release = store.try_autosave_lock(path)
    assert release is not None
    release()
    assert lock_path.stat().st_ino == inode


@pytest.mark.parametrize("publisher", ["checkpoint", "journal"])
def test_autosave_lock_checks_the_newest_durable_sequence(
    tmp_path: Path, publisher: str,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()
    messages = [{"role": "user", "content": "saved"}]
    if publisher == "checkpoint":
        store.checkpoint_snapshot(path, messages, meta={}, sequence=5)
    else:
        store.append_snapshot_delta(path, sequence=5, replace_from=0, messages=messages, meta={})
        store.append_snapshot_delta(path, sequence=3, replace_from=0, messages=messages, meta={})
        with Path(f"{path}.journal").open("ab") as journal:
            journal.write(b'{"journal_version":1,"sequence":9}')

    store = SessionStore()
    with pytest.raises(SnapshotSequenceConflict, match="sequence 4; persisted sequence 5"):
        store.try_autosave_lock(path, expected_sequence=4)

    for expected_sequence in (None, 5, 6):
        release = store.try_autosave_lock(path, expected_sequence=expected_sequence)
        assert release is not None
        release()


def test_autosave_lock_releases_after_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()

    def fail_read(_path: str) -> int:
        raise PermissionError(errno.EACCES, "could not read persisted sequence")

    monkeypatch.setattr(store, "_newest_persisted_sequence", fail_read)
    with pytest.raises(PermissionError, match="could not read persisted sequence"):
        store.try_autosave_lock(path, expected_sequence=0)

    release = store.try_autosave_lock(path)
    assert release is not None
    release()


@pytest.mark.parametrize("publisher", ["checkpoint", "journal"])
def test_autosave_lock_reuses_native_appends_and_detects_peer_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publisher: str,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()
    messages = [{"role": "user", "content": "saved"}]
    release = store.try_autosave_lock(path, expected_sequence=0)
    assert release is not None
    release()

    def unexpected_read(_path: str) -> int:
        pytest.fail("unchanged files or a tracked append should reuse the durable cursor")

    with monkeypatch.context() as cached:
        cached.setattr(store, "_newest_persisted_sequence", unexpected_read)
        store.append_snapshot_delta(path, sequence=5, replace_from=0, messages=messages, meta={})
        store.append_snapshot_delta(path, sequence=3, replace_from=0, messages=messages, meta={})

        with pytest.raises(SnapshotSequenceConflict, match="sequence 4; persisted sequence 5"):
            store.try_autosave_lock(path, expected_sequence=4)
        release = store.try_autosave_lock(path, expected_sequence=5)
        assert release is not None
        release()

    peer = SessionStore()
    if publisher == "checkpoint":
        peer.checkpoint_snapshot(path, messages, meta={}, sequence=6)
    else:
        peer.append_snapshot_delta(path, sequence=6, replace_from=0, messages=messages, meta={})
    with pytest.raises(SnapshotSequenceConflict, match="sequence 5; persisted sequence 6"):
        store.try_autosave_lock(path, expected_sequence=5)


@pytest.mark.parametrize("operation", ["checkpoint", "load"])
def test_autosave_lock_reuses_cursor_from_locked_storage_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()
    messages = [{"role": "user", "content": "saved"}]
    if operation == "checkpoint":
        store.checkpoint_snapshot(path, messages, meta={}, sequence=4)
    else:
        SessionStore().checkpoint_snapshot(path, messages, meta={}, sequence=4)
        assert store.load_snapshot(path, "")["messages"] == messages

    def unexpected_read(_path: str) -> int:
        pytest.fail("checkpoint or load should remember the durable cursor")

    monkeypatch.setattr(store, "_newest_persisted_sequence", unexpected_read)
    with pytest.raises(SnapshotSequenceConflict, match="sequence 3; persisted sequence 4"):
        store.try_autosave_lock(path, expected_sequence=3)


def test_autosave_ownership_and_checkpoint_survive_cache_refresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()
    messages = [{"role": "user", "content": "saved"}]

    release = store.try_autosave_lock(path, expected_sequence=0)
    assert release is not None
    release()

    def fail_stat(_path: str) -> None:
        raise OSError(errno.EIO, "could not check file revisions")

    monkeypatch.setattr(store, "_autosave_file_revisions", fail_stat)
    store.checkpoint_snapshot(path, messages, meta={}, sequence=4)
    with pytest.raises(SnapshotSequenceConflict, match="sequence 3; persisted sequence 4"):
        store.try_autosave_lock(path, expected_sequence=3)
    assert SessionStore().load_snapshot(path, "")["messages"] == messages


def test_autosave_ownership_observes_an_explicit_overwrite(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "session.json")
    store = SessionStore()
    peer = SessionStore()
    messages = [{"role": "user", "content": "saved"}]
    store.checkpoint_snapshot(path, messages, meta={}, sequence=5)

    release = peer.try_autosave_lock(path)
    assert release is not None
    try:
        peer.save(path, messages)
    finally:
        release()

    release = store.try_autosave_lock(path, expected_sequence=0)
    assert release is not None
    release()


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
