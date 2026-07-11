from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import opencollab.adapters.event_log as event_log_mod
import opencollab.adapters.safe_files as safe_files_mod
import pytest
from opencollab.adapters.event_log import JsonlEventSink
from opencollab.adapters.safe_files import append_regular_text, read_regular_bytes
from opencollab.adapters.trace import Tracer

_LOCK_HOLDER = """
import fcntl
import pathlib
import sys
import time

path = sys.argv[1]
ready = pathlib.Path(sys.argv[2])
with open(path, "a", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    ready.write_text("locked", encoding="utf-8")
    time.sleep(10)
"""


async def _wait_until_locked(ready, process):
    deadline = asyncio.get_running_loop().time() + 2.0
    while not ready.exists():
        if process.poll() is not None:
            raise AssertionError(f"lock holder exited with {process.returncode}")
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("lock holder did not acquire the file lock")
        await asyncio.sleep(0.01)


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _spawn_lock_holder(path, ready):
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(path), str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_tracer_rejects_fifo_without_blocking(tmp_path):
    path = tmp_path / "run.jsonl"
    os.mkfifo(path)

    started = time.monotonic()
    with pytest.raises(OSError, match="not a regular file"):
        Tracer("run", output_dir=str(tmp_path))

    assert time.monotonic() - started < 0.5


def test_tracer_rejects_final_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    (tmp_path / "run.jsonl").symlink_to(target)

    with pytest.raises(OSError, match="not a regular file"):
        Tracer("run", output_dir=str(tmp_path))

    assert target.read_text(encoding="utf-8") == "original"


def test_tracer_rejects_symlinked_parent_directory(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(OSError):
        Tracer("run", output_dir=str(linked_parent))

    assert not (real_parent / "run.jsonl").exists()


def test_tracer_does_not_create_through_symlinked_intermediate_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="not a real directory"):
        Tracer("run", output_dir=str(linked_parent / "new"))

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "filename",
    ["../escape.jsonl", "/tmp/escape", "a/b", "line\nbreak", "bad\ud800"],
)
def test_tracer_rejects_path_bearing_filename(tmp_path, filename):
    with pytest.raises(ValueError, match="safe path component"):
        Tracer("run", output_dir=str(tmp_path), filename=filename)


def test_append_helper_rejects_device_file():
    if not os.path.exists("/dev/null"):
        pytest.skip("device file unavailable")
    with pytest.raises(OSError, match="not a regular file"):
        append_regular_text("/dev/null", "must not write")


def test_append_helper_reopens_if_existing_path_changes_after_open(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "events.jsonl"
    path.write_text("old\n", encoding="utf-8")
    detached = tmp_path / "detached.jsonl"
    real_open = safe_files_mod.os.open
    swapped = False

    def swapping_open(name, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(name, flags, *args, **kwargs)
        if (
            not swapped
            and name == path.name
            and (flags & os.O_ACCMODE) == os.O_WRONLY
        ):
            path.rename(detached)
            path.write_text("replacement\n", encoding="utf-8")
            swapped = True
        return fd

    monkeypatch.setattr(safe_files_mod.os, "open", swapping_open)

    append_regular_text(path, "record\n")

    assert swapped is True
    assert detached.read_text(encoding="utf-8") == "old\n"
    assert path.read_text(encoding="utf-8") == "replacement\nrecord\n"


def test_append_helper_reopens_if_new_path_changes_after_create(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "events.jsonl"
    detached = tmp_path / "detached.jsonl"
    real_open = safe_files_mod.os.open
    swapped = False

    def swapping_open(name, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(name, flags, *args, **kwargs)
        if (
            not swapped
            and name == path.name
            and (flags & os.O_ACCMODE) == os.O_WRONLY
        ):
            path.rename(detached)
            path.write_text("replacement\n", encoding="utf-8")
            swapped = True
        return fd

    monkeypatch.setattr(safe_files_mod.os, "open", swapping_open)

    append_regular_text(path, "record\n")

    assert swapped is True
    assert detached.read_bytes() == b""
    assert path.read_text(encoding="utf-8") == "replacement\nrecord\n"


def test_append_helper_reopens_if_parent_path_changes_after_open(
    tmp_path,
    monkeypatch,
):
    parent = tmp_path / "observability"
    parent.mkdir()
    path = parent / "events.jsonl"
    path.write_text("old\n", encoding="utf-8")
    old_parent = tmp_path / "observability-old"
    real_open = safe_files_mod.os.open
    swapped = False

    def swapping_open(name, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(name, flags, *args, **kwargs)
        if (
            not swapped
            and name == path.name
            and (flags & os.O_ACCMODE) == os.O_WRONLY
        ):
            parent.rename(old_parent)
            parent.mkdir()
            path.write_text("replacement\n", encoding="utf-8")
            swapped = True
        return fd

    monkeypatch.setattr(safe_files_mod.os, "open", swapping_open)

    append_regular_text(path, "record\n")

    assert swapped is True
    assert (old_parent / path.name).read_text(encoding="utf-8") == "old\n"
    assert path.read_text(encoding="utf-8") == "replacement\nrecord\n"


def test_read_helper_rejects_parent_path_change_during_read(tmp_path, monkeypatch):
    parent = tmp_path / "state"
    parent.mkdir()
    path = parent / "snapshot.json"
    path.write_bytes(b"old")
    old_parent = tmp_path / "state-old"
    real_read = safe_files_mod.os.read
    swapped = False

    def swapping_read(fd, size):
        nonlocal swapped
        payload = real_read(fd, size)
        if not swapped and payload:
            parent.rename(old_parent)
            parent.mkdir()
            path.write_bytes(b"replacement")
            swapped = True
        return payload

    monkeypatch.setattr(safe_files_mod.os, "read", swapping_read)

    with pytest.raises(OSError, match="parent changed"):
        read_regular_bytes(path, max_bytes=1024)

    assert swapped is True
    assert (old_parent / path.name).read_bytes() == b"old"
    assert path.read_bytes() == b"replacement"


def test_append_helper_reports_parent_change_during_write(tmp_path, monkeypatch):
    parent = tmp_path / "observability"
    parent.mkdir()
    path = parent / "events.jsonl"
    path.touch()
    old_parent = tmp_path / "observability-old"
    real_write = safe_files_mod.write_locked_text
    swapped = False

    def swapping_write(handle, text):
        nonlocal swapped
        real_write(handle, text)
        parent.rename(old_parent)
        parent.mkdir()
        swapped = True

    monkeypatch.setattr(safe_files_mod, "write_locked_text", swapping_write)

    with pytest.raises(OSError, match="changed while writing"):
        append_regular_text(path, "record\n")

    assert swapped is True
    assert (old_parent / path.name).read_text(encoding="utf-8") == "record\n"
    assert list(parent.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
async def test_event_sink_fifo_returns_without_freezing_loop(tmp_path):
    path = tmp_path / "events.fifo"
    os.mkfifo(path)
    sink = JsonlEventSink(str(path))

    await asyncio.wait_for(sink.emit({"type": "test"}), timeout=0.5)
    assert path.is_fifo()


@pytest.mark.asyncio
async def test_event_sink_rejects_final_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "events.jsonl"
    link.symlink_to(target)
    sink = JsonlEventSink(str(link))

    await asyncio.wait_for(sink.emit({"type": "test"}), timeout=0.5)

    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_event_sink_rejects_symlinked_parent_directory(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    sink = JsonlEventSink(str(linked_parent / "events.jsonl"))

    await asyncio.wait_for(sink.emit(SimpleNamespace(type="event", data={})), 0.5)

    assert not (real_parent / "events.jsonl").exists()


@pytest.mark.asyncio
async def test_event_sink_does_not_create_through_symlinked_intermediate_parent(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)
    sink = JsonlEventSink(str(linked_parent / "new" / "events.jsonl"))

    await sink.emit(SimpleNamespace(type="event", data={}))

    assert list(outside.iterdir()) == []
    assert sink.write_error is not None
    assert sink.dropped_events == 1


def test_tracer_reports_discontinuity_if_path_changes_before_next_record(tmp_path):
    tracer = Tracer("run", output_dir=str(tmp_path))
    path = tmp_path / "run.jsonl"
    detached = tmp_path / "detached.jsonl"
    path.rename(detached)
    path.touch()

    tracer.log_step("tool_exec", {"tool": "test"})
    tracer.close()

    assert detached.read_bytes() == b""
    assert path.read_bytes() == b""
    assert tracer.write_error is not None
    assert "trace target changed before writing" in tracer.write_error
    assert tracer.dropped_steps == 1


def test_tracer_reports_same_inode_truncation_between_records(tmp_path):
    tracer = Tracer("run", output_dir=str(tmp_path))
    path = tmp_path / "run.jsonl"
    tracer.log_step("first", {})
    path.write_bytes(b"")

    tracer.log_step("second", {})
    tracer.close()

    assert path.read_bytes() == b""
    assert tracer.write_error is not None
    assert "trace target changed between records" in tracer.write_error
    assert tracer.dropped_steps == 1


def test_tracer_write_failure_is_sticky_and_does_not_escape(tmp_path, monkeypatch):
    tracer = Tracer("run", output_dir=str(tmp_path))

    def fail_write(_handle, _text):
        raise OSError("disk full")

    monkeypatch.setattr("opencollab.adapters.trace.write_locked_text", fail_write)

    tracer.log_step("llm_call", {"model": "test"})
    tracer.log_step("tool_exec", {"tool": "test"})

    assert tracer.write_error == "OSError: disk full"
    assert tracer.dropped_steps == 2


@pytest.mark.asyncio
async def test_event_sink_concurrent_records_remain_whole_json_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    payload = "x" * 100_000

    await asyncio.gather(
        *(sink.emit(SimpleNamespace(type="event", data={"index": index, "blob": payload}))
          for index in range(20))
    )

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 20
    assert {row["data"]["index"] for row in rows} == set(range(20))


@pytest.mark.asyncio
async def test_event_sink_reports_file_replacement_between_records(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    await sink.emit(SimpleNamespace(type="first", data={}))
    detached = tmp_path / "detached.jsonl"
    path.rename(detached)
    path.touch()

    await sink.emit(SimpleNamespace(type="second", data={}))

    assert len(detached.read_text(encoding="utf-8").splitlines()) == 1
    assert path.read_bytes() == b""
    assert sink.write_error is not None
    assert "path changed between records" in sink.write_error
    assert sink.dropped_events == 1


@pytest.mark.asyncio
async def test_event_sink_reports_truncation_between_records(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    await sink.emit(SimpleNamespace(type="first", data={}))
    path.write_bytes(b"")

    await sink.emit(SimpleNamespace(type="second", data={}))

    assert path.read_bytes() == b""
    assert sink.write_error is not None
    assert "truncated between records" in sink.write_error
    assert sink.dropped_events == 1


@pytest.mark.asyncio
async def test_event_sink_reports_same_size_rewrite_between_records(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    await sink.emit(SimpleNamespace(type="first", data={}))
    original_size = path.stat().st_size
    path.write_bytes(b"x" * original_size)

    await sink.emit(SimpleNamespace(type="second", data={}))

    assert path.read_bytes() == b"x" * original_size
    assert sink.write_error is not None
    assert "modified between records" in sink.write_error
    assert sink.dropped_events == 1


@pytest.mark.asyncio
async def test_event_sink_returns_promptly_while_real_process_holds_lock(tmp_path):
    path = tmp_path / "events.jsonl"
    path.touch()
    ready = tmp_path / "locked"
    process = _spawn_lock_holder(path, ready)
    try:
        await _wait_until_locked(ready, process)
        sink = JsonlEventSink(str(path))

        started = time.monotonic()
        await asyncio.wait_for(sink.emit({"type": "blocked"}), timeout=0.5)

        assert time.monotonic() - started < 0.4
        assert path.read_text(encoding="utf-8") == ""
    finally:
        _stop_process(process)


@pytest.mark.asyncio
async def test_event_sink_daemon_io_timeout_cannot_hold_event_loop(
    tmp_path,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    def block_forever(*_args):
        entered.set()
        release.wait()

    monkeypatch.setattr(event_log_mod, "_append_event_record", block_forever)
    monkeypatch.setattr(event_log_mod, "EVENT_IO_TIMEOUT_SECONDS", 0.01)
    sink = JsonlEventSink(str(tmp_path / "events.jsonl"))

    started = time.monotonic()
    await asyncio.wait_for(sink.emit({"type": "blocked"}), timeout=0.5)

    assert entered.wait(timeout=0.5)
    assert time.monotonic() - started < 0.5
    assert sink.dropped_events == 1
    assert sink.write_error is not None
    release.set()


@pytest.mark.asyncio
async def test_tracer_records_busy_lock_without_blocking_event_loop(tmp_path):
    tracer = Tracer("run", output_dir=str(tmp_path))
    ready = tmp_path / "locked"
    process = _spawn_lock_holder(tmp_path / "run.jsonl", ready)
    try:
        await _wait_until_locked(ready, process)

        started = time.monotonic()
        tracer.log_step("blocked", {})

        assert time.monotonic() - started < 0.4
        assert tracer.write_error is not None
        assert tracer.write_error.startswith("BlockingIOError:")
        assert tracer.dropped_steps == 1
    finally:
        _stop_process(process)
        tracer.close()
