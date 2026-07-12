from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "glm_token_monitor.py"
SPEC = importlib.util.spec_from_file_location("glm_token_monitor", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


@pytest.mark.parametrize("value", ["inf", "nan", "-1", "invalid"])
def test_price_and_watch_arguments_reject_invalid_numbers(value):
    with pytest.raises(Exception):
        monitor._nonnegative_float_arg(value)


def test_collect_ignores_non_finite_and_negative_usage(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        '{"type":"llm_call","payload":{"model":"glm-5.2",'
        '"usage":{"input_tokens":-1,"output_tokens":"inf"}},'
        '"metrics":{"latency_s":"nan"}}\n',
        encoding="utf-8",
    )

    totals = monitor.collect(tmp_path, "glm-5.2")

    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0
    assert totals["latency_s"] == 0.0


def test_collect_marks_structurally_invalid_json_records_incomplete(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        "[]\n"
        '{"type":"llm_call","payload":"invalid"}\n'
        '{"type":"llm_call","payload":{"model":"glm-5.2",'
        '"usage":"invalid"},"metrics":"invalid"}\n'
        '{"type":"llm_call","payload":{"model":"glm-5.2",'
        '"usage":{"total_tokens":7}}}\n',
        encoding="utf-8",
    )

    totals = monitor.collect(tmp_path, "glm-5.2")

    assert totals["files"] == 0
    assert totals["calls"] == 0
    assert totals["complete"] is False
    assert totals["input_errors"]


def test_iter_records_streams_with_bounded_readline_and_skips_oversized_line(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "large.jsonl"
    path.write_bytes(
        b'{"oversized":"' + b"x" * 500 + b'"}\n'
        b'{"type":"llm_call","payload":{"model":"glm-5.2"}}\n'
    )
    monkeypatch.setattr(monitor, "MAX_TRAJECTORY_RECORD_BYTES", 128)
    original_open = monitor._open_regular_binary
    requested_sizes = []

    class TrackingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._wrapped.close()

        def readline(self, size=-1):
            requested_sizes.append(size)
            assert 0 < size <= 129
            return self._wrapped.readline(size)

        def fileno(self):
            return self._wrapped.fileno()

    @contextmanager
    def tracked_open(target):
        with original_open(target) as wrapped:
            yield TrackingReader(wrapped)

    monkeypatch.setattr(monitor, "_open_regular_binary", tracked_open)
    with pytest.raises(ValueError, match="record exceeds"):
        list(monitor._iter_records(path))

    assert requested_sizes


def test_iter_records_rejects_fifo_and_final_symlink(tmp_path):
    fifo = tmp_path / "trajectory.fifo"
    os.mkfifo(fifo)
    real = tmp_path / "real.jsonl"
    real.write_text('{"type":"llm_call"}\n', encoding="utf-8")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(real)

    with pytest.raises(OSError, match="not a regular file"):
        list(monitor._iter_records(fifo))
    with pytest.raises(OSError, match="not a regular file"):
        list(monitor._iter_records(link))


def test_collect_does_not_follow_trajectory_file_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real.jsonl"
    real.write_text(
        '{"type":"llm_call","payload":{"model":"glm-5.2"}}\n',
        encoding="utf-8",
    )
    root = tmp_path / "trajectories"
    root.mkdir()
    (root / "linked.jsonl").symlink_to(real)

    totals = monitor.collect(root, None)

    assert totals["files"] == 0
    assert totals["calls"] == 0
    assert totals["complete"] is False
    assert totals["input_errors"]


def test_collect_recurses_into_nested_trajectory_directories(tmp_path):
    nested = tmp_path / "run" / "agent"
    nested.mkdir(parents=True)
    (nested / "trajectory.jsonl").write_text(
        json.dumps(
            {
                "type": "llm_call",
                "run_id": "nested-run",
                "payload": {
                    "model": "glm-5.2",
                    "usage": {"input_tokens": 7, "output_tokens": 5},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    totals = monitor.collect(tmp_path, "glm-5.2")

    assert totals["complete"] is True
    assert totals["files"] == 1
    assert totals["runs"] == 1
    assert totals["calls"] == 1
    assert totals["split_total_tokens"] == 12


def test_collect_rejects_nested_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "trajectory.jsonl").write_text(
        '{"type":"llm_call"}\n',
        encoding="utf-8",
    )
    root = tmp_path / "trajectories"
    root.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="contains a symlink"):
        monitor.collect(root, None)


def test_collect_falls_back_to_input_plus_output_when_total_tokens_missing(tmp_path):
    (tmp_path / "run.jsonl").write_text(
        json.dumps(
            {
                "type": "llm_call",
                "payload": {
                    "model": "glm-5.2",
                    "usage": {"input_tokens": 11, "output_tokens": 13},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    totals = monitor.collect(tmp_path, "glm-5.2")

    assert totals["split_total_tokens"] == 24


def test_trajectory_directory_enumeration_is_bounded(tmp_path, monkeypatch):
    for index in range(4):
        (tmp_path / f"entry-{index}").touch()
    monkeypatch.setattr(monitor, "MAX_TRAJECTORY_DIRECTORY_ENTRIES", 3)

    with pytest.raises(ValueError, match="entries exceed limit"):
        monitor.collect(tmp_path, None)


def test_collect_rejects_symlink_trajectory_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        monitor.collect(linked, None)


def test_collect_rejects_symlinked_trajectory_directory_ancestor(tmp_path):
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        monitor.collect(linked / "nested", None)


def test_collect_rejects_oversized_trajectory_file(tmp_path, monkeypatch):
    path = tmp_path / "run.jsonl"
    path.write_text('{"type":"llm_call","blob":"' + "x" * 100 + '"}\n')
    monkeypatch.setattr(monitor, "MAX_TRAJECTORY_FILE_BYTES", 32)

    totals = monitor.collect(tmp_path, None)

    assert totals["complete"] is False
    assert any("trajectory exceeds" in error for error in totals["input_errors"])


def test_collect_marks_mid_read_error_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "run.jsonl"
    path.write_text(
        '{"type":"llm_call","payload":{"model":"glm-5.2"}}\n'
        '{"type":"llm_call","payload":{"model":"glm-5.2"}}\n',
        encoding="utf-8",
    )
    original_open = monitor._open_regular_binary

    class FailingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._reads = 0

        def fileno(self):
            return self._wrapped.fileno()

        def readline(self, size=-1):
            self._reads += 1
            if self._reads > 1:
                raise OSError("synthetic mid-read failure")
            return self._wrapped.readline(size)

    @contextmanager
    def failing_open(target):
        with original_open(target) as wrapped:
            yield FailingReader(wrapped)

    monkeypatch.setattr(monitor, "_open_regular_binary", failing_open)

    totals = monitor.collect(tmp_path, None)

    assert totals["calls"] == 1
    assert totals["complete"] is False
    assert any("mid-read failure" in error for error in totals["input_errors"])


def test_collect_rejects_growth_after_total_byte_accounting(tmp_path, monkeypatch):
    path = tmp_path / "run.jsonl"
    path.write_text(
        '{"type":"llm_call","payload":{"model":"glm-5.2"}}\n',
        encoding="utf-8",
    )
    original_iter = monitor._iter_records
    mutated = False

    def mutate_before_read(target, *, expected_size=None):
        nonlocal mutated
        if not mutated:
            mutated = True
            with target.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"llm_call","payload":{"model":"glm-5.2"}}\n'
                )
        yield from original_iter(target, expected_size=expected_size)

    monkeypatch.setattr(monitor, "_iter_records", mutate_before_read)

    totals = monitor.collect(tmp_path, None)

    assert totals["complete"] is False
    assert totals["calls"] == 0
    assert any("between accounting and read" in error for error in totals["input_errors"])


def test_collect_total_record_limit_is_explicit(tmp_path, monkeypatch):
    (tmp_path / "run.jsonl").write_text(
        '{"type":"event"}\n{"type":"event"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "MAX_TRAJECTORY_TOTAL_RECORDS", 1)

    totals = monitor.collect(tmp_path, None)

    assert totals["complete"] is False
    assert any("total record limit" in error for error in totals["input_errors"])


def test_glm_monitor_cli_returns_nonzero_for_incomplete_input(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text('{"type":"llm_call"}\n', encoding="utf-8")
    (tmp_path / "linked.jsonl").symlink_to(real)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--trajectories-dir",
            str(tmp_path),
            "--model",
            "",
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    assert "input_status: incomplete" in result.stdout
