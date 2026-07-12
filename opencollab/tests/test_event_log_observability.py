from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.adapters.event_log import JsonlEventSink


class _CustomObject:
    def __str__(self) -> str:
        return "custom-object"


def _load_loop_monitor_module():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "swebench_loop_monitor.py"
    spec = importlib.util.spec_from_file_location("swebench_loop_monitor", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jsonl_event_sink_serializes_custom_event_data(tmp_path):
    path = tmp_path / "events" / "run.jsonl"
    sink = JsonlEventSink(str(path))

    asyncio.run(sink.emit(SimpleNamespace(type="custom", data={"value": _CustomObject()})))

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["type"] == "custom"
    assert row["data"]["value"] == "custom-object"


def test_jsonl_event_sink_remains_passive_when_parent_cannot_be_created(tmp_path):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("occupied", encoding="utf-8")
    sink = JsonlEventSink(str(blocker / "run.jsonl"))

    asyncio.run(sink.emit(SimpleNamespace(type="custom", data={"value": "ok"})))

    assert blocker.read_text(encoding="utf-8") == "occupied"
    assert sink.write_error is not None
    assert sink.dropped_events == 1


def test_loop_monitor_truncate_obj_returns_json_safe_value():
    monitor = _load_loop_monitor_module()

    value = {"args": {"value": _CustomObject()}}
    result = monitor._truncate_obj(value)

    assert result == {"args": {"value": "custom-object"}}
    json.dumps(result)


def test_loop_monitor_json_and_jsonl_reads_are_byte_bounded(tmp_path, monkeypatch):
    monitor = _load_loop_monitor_module()
    monkeypatch.setattr(monitor, "MAX_SESSION_JSON_BYTES", 64)
    monkeypatch.setattr(monitor, "MAX_JSONL_LINE_BYTES", 64)
    oversized_json = tmp_path / "agent.json"
    oversized_json.write_bytes(b'{"messages":"' + b"x" * 100 + b'"}')
    events = tmp_path / "events.jsonl"
    events.write_bytes(
        b'{"oversized":"' + b"x" * 100 + b'"}\n'
        b'{"type":"loop_detected","data":{"tool":"exec"}}\n'
    )
    original_open = monitor._open_regular_binary
    read_sizes = []
    readline_sizes = []

    class TrackingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._wrapped.close()

        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 < size <= 65
            return self._wrapped.read(size)

        def readline(self, size=-1):
            readline_sizes.append(size)
            assert 0 < size <= 65
            return self._wrapped.readline(size)

        def fileno(self):
            return self._wrapped.fileno()

    @contextmanager
    def tracked_open(path):
        with original_open(path) as wrapped:
            yield TrackingReader(wrapped)

    monkeypatch.setattr(monitor, "_open_regular_binary", tracked_open)
    assert monitor._load_json(oversized_json) is None
    with pytest.raises(ValueError, match="record exceeds"):
        list(monitor._iter_jsonl(events))

    assert read_sizes == []
    assert readline_sizes


def test_loop_monitor_streams_all_loop_counts_but_bounds_text_and_recent_events(
    tmp_path,
    monkeypatch,
):
    monitor = _load_loop_monitor_module()
    monkeypatch.setattr(monitor, "MAX_ACTIVE_TEXT_CHARS", 80)
    root = tmp_path / "session"
    root.mkdir()
    events = root / "events.jsonl"
    rows = []
    for index in range(25):
        rows.append(
            {
                "type": "loop_detected",
                "data": {"tool": "exec", "count": index + 1},
            }
        )
    rows.extend(
        [
            {"type": "text_delta", "data": {"aid": "a", "content": "x" * 40}},
            {"type": "text_delta", "data": {"aid": "a", "content": "y" * 80}},
            {"type": "step_end", "data": {"aid": "a"}},
        ]
    )
    events.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    analysis, used = monitor._discover_event_analysis(root, [])

    assert analysis["loop"]["loop_detected_count"] == 25
    assert analysis["loop"]["max_tool_loop_count"] == 25
    assert len(analysis["loop"]["recent_loop_events"]) == 5
    recent_text = analysis["text"]["recent_assistant_texts"][-1]["text"]
    assert len(recent_text) <= 80
    assert used == [str(events.resolve())]


def test_loop_monitor_bounds_retained_messages_and_message_content(
    tmp_path,
    monkeypatch,
):
    monitor = _load_loop_monitor_module()
    monkeypatch.setattr(monitor, "MAX_RETAINED_MESSAGES", 2)
    monkeypatch.setattr(monitor, "MAX_MESSAGE_TEXT_CHARS", 10)
    monkeypatch.setattr(monitor, "MAX_TOOL_CALLS_PER_MESSAGE", 1)
    monkeypatch.setattr(monitor, "MAX_RETAINED_TOOL_CALLS", 1)
    root = tmp_path / "session"
    root.mkdir()
    agent = root / "agent_a.json"
    agent.write_text(
        json.dumps(
            {
                "aid": "a",
                "role": "coder",
                "messages": [
                    {"role": "assistant", "content": "first"},
                    {"role": "assistant", "content": "second-message-long"},
                    {
                        "role": "assistant",
                        "content": "third-message-long",
                        "tool_calls": [
                            {"id": "old", "function": {"name": "x", "arguments": "a"}},
                            {"id": "new", "function": {"name": "y", "arguments": "b"}},
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    messages, used = monitor._session_messages(root)

    assert len(messages) == 2
    assert all(len(message["content"]) <= 10 for message in messages)
    assert messages[-1]["tool_calls"][0]["id"] == "new"
    assert sum(len(message.get("tool_calls") or []) for message in messages) == 1
    assert used == [str(agent)]


def test_loop_monitor_caps_sentence_key_length(monkeypatch):
    monitor = _load_loop_monitor_module()
    monkeypatch.setattr(monitor, "MAX_SENTENCE_CHARS", 32)
    counter = monitor.Counter()

    monitor._update_sentence_counter(counter, "x" * 10_000)

    assert len(counter) == 1
    assert len(next(iter(counter))) == 32


def test_bounded_tail_reader_seeks_and_reads_only_requested_bytes(
    tmp_path,
    monkeypatch,
):
    monitor = _load_loop_monitor_module()
    path = tmp_path / "large.log"
    path.write_bytes(b"a" * 1_000_000 + b"level=critical")
    original_open = monitor._open_regular_binary
    read_sizes = []

    class TrackingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._wrapped.close()

        def seek(self, *args):
            return self._wrapped.seek(*args)

        def tell(self):
            return self._wrapped.tell()

        def read(self, size=-1):
            read_sizes.append(size)
            assert size == 64
            return self._wrapped.read(size)

    @contextmanager
    def tracked_open(target):
        with original_open(target) as wrapped:
            yield TrackingReader(wrapped)

    monkeypatch.setattr(monitor, "_open_regular_binary", tracked_open)
    tail = monitor._read_tail_text(path, 64)

    assert tail.endswith("level=critical")
    assert read_sizes == [64]


def test_loop_monitor_rejects_fifo_and_final_symlink_inputs(tmp_path):
    monitor = _load_loop_monitor_module()
    fifo = tmp_path / "events.fifo"
    os.mkfifo(fifo)
    real = tmp_path / "events.jsonl"
    real.write_text('{"type":"ok"}\n', encoding="utf-8")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(real)

    assert monitor._read_tail_text(fifo, 64) == ""
    assert monitor._load_json(fifo) is None
    with pytest.raises(OSError, match="not a regular file"):
        list(monitor._iter_jsonl(fifo))
    assert monitor._read_tail_text(link, 64) == ""
    assert monitor._load_json(link) is None
    with pytest.raises(OSError, match="not a regular file"):
        list(monitor._iter_jsonl(link))


def test_loop_monitor_event_discovery_does_not_resolve_symlink(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside.events.jsonl"
    outside.write_text(
        '{"type":"loop_detected","data":{"tool":"exec","count":99}}\n',
        encoding="utf-8",
    )
    (root / "events.jsonl").symlink_to(outside)

    analysis, used = monitor._discover_event_analysis(root, [])

    assert analysis["loop"]["loop_detected_count"] == 0
    assert used == []
    assert analysis["input_errors"]


def test_loop_monitor_build_report_rejects_symlink_session_root(tmp_path):
    monitor = _load_loop_monitor_module()
    real = tmp_path / "real-session"
    real.mkdir()
    linked = tmp_path / "linked-session"
    linked.symlink_to(real, target_is_directory=True)
    args = SimpleNamespace(
        session_root=str(linked),
        events_file=[],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    with pytest.raises(ValueError, match="session root is not a real directory"):
        monitor.build_report(args)


def test_loop_monitor_rejects_symlinked_session_root_ancestor(tmp_path):
    monitor = _load_loop_monitor_module()
    outside = tmp_path / "outside"
    session = outside / "session"
    session.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    args = SimpleNamespace(
        session_root=str(linked / "session"),
        events_file=[],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    with pytest.raises(OSError):
        monitor.build_report(args)


def test_loop_monitor_explicit_event_rejects_symlinked_ancestor(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "events.jsonl").write_text(
        '{"type":"loop_detected","data":{"count":99}}\n',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    analysis, used = monitor._discover_event_analysis(
        root,
        [str(linked / "events.jsonl")],
    )

    assert used == []
    assert analysis["loop"]["loop_detected_count"] == 0
    assert analysis["input_errors"]


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_loop_monitor_critical_report_rejects_unsafe_diff_input(tmp_path, kind):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    events = root / "events.jsonl"
    events.write_text(
        "".join(
            json.dumps(
                {"type": "loop_detected", "data": {"tool": "exec", "count": index}}
            )
            + "\n"
            for index in range(1, 13)
        ),
        encoding="utf-8",
    )
    diff = tmp_path / "candidate.patch"
    if kind == "fifo":
        os.mkfifo(diff)
    else:
        real = tmp_path / "real.patch"
        real.write_text("+unsafe\n", encoding="utf-8")
        diff.symlink_to(real)
    output = tmp_path / "monitor.json"
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=str(diff),
        instance_id="task",
        output=str(output),
    )

    report = monitor.build_report(args)

    assert report["level"] == "critical"
    assert report["diff_bytes"] == 0
    assert report["diff_input_error"]
    assert "current_git_diff" not in report["artifacts"]


def test_loop_monitor_directory_enumeration_is_bounded(tmp_path, monkeypatch):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    for index in range(4):
        (root / f"entry-{index}").touch()
    monkeypatch.setattr(monitor, "MAX_SESSION_TREE_ENTRIES", 3)

    with pytest.raises(ValueError, match="entries exceed limit"):
        monitor._event_paths(root, [])


def test_loop_monitor_total_event_input_is_bounded(tmp_path, monkeypatch):
    monitor = _load_loop_monitor_module()
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"event","data":"' + "x" * 100 + '"}\n')
    monkeypatch.setattr(monitor, "MAX_EVENT_JSONL_BYTES", 32)

    with pytest.raises(ValueError, match="event JSONL exceeds"):
        list(monitor._iter_jsonl(events))


def test_loop_monitor_rejects_event_growth_after_byte_accounting(
    tmp_path,
    monkeypatch,
):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    events = root / "events.jsonl"
    events.write_text('{"type":"ok"}\n', encoding="utf-8")
    original_iter = monitor._iter_jsonl
    mutated = False

    def mutate_before_read(path, *, expected_size=None):
        nonlocal mutated
        if not mutated:
            mutated = True
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"loop_detected","data":{"count":99}}\n')
        yield from original_iter(path, expected_size=expected_size)

    monkeypatch.setattr(monitor, "_iter_jsonl", mutate_before_read)

    analysis, used = monitor._discover_event_analysis(root, [])

    assert used == []
    assert analysis["loop"]["loop_detected_count"] == 0
    assert any("between accounting and read" in error for error in analysis["input_errors"])


def test_loop_monitor_empty_observation_root_is_incomplete(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "empty-session"
    root.mkdir()
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["level"] == "warn"
    assert report["input_complete"] is False
    assert "input_incomplete" in report["reasons"]


def test_loop_monitor_diff_only_is_incomplete(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "empty-session"
    root.mkdir()
    diff = tmp_path / "candidate.patch"
    diff.write_text("+change\n", encoding="utf-8")
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=str(diff),
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["diff_bytes"] > 0
    assert report["input_complete"] is False
    assert report["level"] == "warn"
    assert "input_incomplete" in report["reasons"]


def test_loop_monitor_scandir_failure_is_recorded_as_input_error(
    tmp_path,
    monkeypatch,
):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    original_scandir = monitor.os.scandir

    def fail_root(path):
        if Path(path) == root:
            raise PermissionError("enumeration denied")
        return original_scandir(path)

    monkeypatch.setattr(monitor.os, "scandir", fail_root)
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["input_complete"] is False
    assert any("enumeration denied" in error for error in report["input_errors"])


def test_loop_monitor_atomic_output_rejects_symlinked_parent(tmp_path):
    monitor = _load_loop_monitor_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        monitor._atomic_write_bytes(linked / "report.json", b"{}\n")

    assert list(outside.iterdir()) == []


def test_loop_monitor_critical_artifacts_reject_symlink_directory(tmp_path):
    monitor = _load_loop_monitor_module()
    output = tmp_path / "monitor.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    monitor._artifact_dir(output).symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        monitor._write_critical_artifacts(
            output,
            diff_payload=b"+change\n",
            write_report={
                "last_successful_write": None,
                "recent_tool_errors": [],
            },
            text_report={"recent_assistant_texts": []},
        )

    assert list(outside.iterdir()) == []


def test_loop_monitor_missing_explicit_diff_is_incomplete(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    (root / "agent_0_lead.json").write_text(
        json.dumps({"role": "lead", "messages": []}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=str(tmp_path / "missing.patch"),
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["level"] == "warn"
    assert report["input_complete"] is False
    assert report["diff_input_error"].startswith("FileNotFoundError:")


def test_loop_monitor_damaged_session_file_is_incomplete(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    (root / "agent_0_lead.json").write_text("{broken", encoding="utf-8")
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["level"] == "warn"
    assert report["input_complete"] is False
    assert report["session_files_discovered"] == 1
    assert report["input_errors"]


def test_loop_monitor_total_session_bytes_are_bounded(tmp_path, monkeypatch):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    for index in range(2):
        (root / f"agent_{index}.json").write_text(
            json.dumps({"role": "worker", "messages": []}),
            encoding="utf-8",
        )
    monkeypatch.setattr(monitor, "MAX_SESSION_TOTAL_BYTES", 32)

    messages, used, errors, discovered = monitor._session_messages_status(root)

    assert messages == []
    assert used == []
    assert discovered == 2
    assert any("total byte limit" in error for error in errors)


def test_loop_monitor_missing_explicit_event_is_not_hidden_by_valid_session(tmp_path):
    monitor = _load_loop_monitor_module()
    root = tmp_path / "session"
    root.mkdir()
    (root / "agent_0_lead.json").write_text(
        json.dumps({"role": "lead", "messages": []}),
        encoding="utf-8",
    )
    missing = root / "events.jsonl"
    args = SimpleNamespace(
        session_root=str(root),
        events_file=[str(missing)],
        diff_file=None,
        instance_id="task",
        output=str(tmp_path / "report.json"),
    )

    report = monitor.build_report(args)

    assert report["level"] == "warn"
    assert report["input_complete"] is False
    assert any(str(missing) in error for error in report["input_errors"])
