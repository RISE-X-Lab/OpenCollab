"""Observable file behavior without syscall-race fixtures."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from opencollab.adapters.event_log import JsonlEventSink
from opencollab.adapters.safe_files import append_regular_text
from opencollab.adapters.trace import Tracer


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_tracer_rejects_fifo_without_blocking(tmp_path) -> None:
    os.mkfifo(tmp_path / "run.jsonl")
    with pytest.raises(OSError, match="regular file"):
        Tracer("run", output_dir=str(tmp_path))


def test_tracer_rejects_symlink_target_and_parent(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("original", encoding="utf-8")
    (tmp_path / "run.jsonl").symlink_to(target)
    with pytest.raises(OSError):
        Tracer("run", output_dir=str(tmp_path))
    assert target.read_text(encoding="utf-8") == "original"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="real directory"):
        Tracer("other", output_dir=str(linked / "new"))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("filename", ["../escape", "/tmp/escape", "a/b", "line\nbreak"])
def test_tracer_rejects_path_bearing_filename(tmp_path, filename) -> None:
    with pytest.raises(ValueError, match="safe path component"):
        Tracer("run", output_dir=str(tmp_path), filename=filename)


def test_append_rejects_device_file() -> None:
    if not os.path.exists("/dev/null"):
        pytest.skip("device file unavailable")
    with pytest.raises(OSError, match="regular file"):
        append_regular_text("/dev/null", "record\n")


def test_tracer_records_valid_jsonl_and_flushes(tmp_path) -> None:
    tracer = Tracer("run", output_dir=str(tmp_path))
    tracer.log_step("tool_exec", {"tool": "test"}, tokens=3, latency=0.25)
    tracer.flush()
    tracer.close()
    row = json.loads((tmp_path / "run.jsonl").read_text(encoding="utf-8"))
    assert row["run_id"] == "run"
    assert row["type"] == "tool_exec"
    assert row["metrics"] == {"tokens": 3, "latency_s": 0.25}


def test_tracer_write_failure_is_sticky(tmp_path, monkeypatch) -> None:
    tracer = Tracer("run", output_dir=str(tmp_path))

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("opencollab.adapters.trace.write_locked_text", fail)
    tracer.log_step("first", {})
    tracer.log_step("second", {})
    assert tracer.write_error == "OSError: disk full"
    assert tracer.dropped_steps == 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
async def test_event_sink_fifo_failure_returns_without_freezing(tmp_path) -> None:
    path = tmp_path / "events.fifo"
    os.mkfifo(path)
    sink = JsonlEventSink(str(path))
    await asyncio.wait_for(sink.emit({"type": "test"}), timeout=0.5)
    assert sink.write_error is not None
    assert sink.dropped_events == 1


async def test_event_sink_rejects_symlink_without_touching_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "events.jsonl"
    link.symlink_to(target)
    sink = JsonlEventSink(str(link))
    await sink.emit(SimpleNamespace(type="event", data={}))
    assert target.read_text(encoding="utf-8") == "original"
    assert sink.write_error is not None


async def test_event_sink_concurrent_records_remain_whole_json_lines(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    await asyncio.gather(
        *(sink.emit(SimpleNamespace(type="event", data={"index": index})) for index in range(30))
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {row["data"]["index"] for row in rows} == set(range(30))
    assert sink.write_error is None
