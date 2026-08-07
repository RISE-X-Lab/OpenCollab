from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import opencollab.adapters.event_log as event_log
from opencollab.adapters.event_log import JsonlEventSink


class _CustomObject:
    def __str__(self) -> str:
        return "custom-object"


def test_jsonl_event_sink_serializes_custom_event_data(tmp_path):
    path = tmp_path / "events" / "run.jsonl"
    sink = JsonlEventSink(str(path))

    asyncio.run(sink.emit(SimpleNamespace(type="custom", data={"value": _CustomObject()})))

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["type"] == "custom"
    assert row["data"]["value"] == "custom-object"


def test_jsonl_event_sink_preserves_plain_mapping_payload(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))

    asyncio.run(sink.emit({"type": "custom", "data": {"value": 7}, "sequence": 3}))

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["type"] == "custom"
    assert row["data"] == {"value": 7}
    assert row["sequence"] == 3


def test_jsonl_event_sink_remains_passive_when_parent_cannot_be_created(tmp_path):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("occupied", encoding="utf-8")
    sink = JsonlEventSink(str(blocker / "run.jsonl"))

    asyncio.run(sink.emit(SimpleNamespace(type="custom", data={"value": "ok"})))

    assert blocker.read_text(encoding="utf-8") == "occupied"
    assert sink.write_error is not None
    assert sink.dropped_events == 1


def test_jsonl_event_sink_preserves_emission_order(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))

    async def scenario():
        for index in range(3):
            await sink.emit(SimpleNamespace(type="ordered", data={"index": index}))

    asyncio.run(scenario())
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["data"]["index"] for row in rows] == [0, 1, 2]


def test_jsonl_event_sink_retains_first_write_error(tmp_path, monkeypatch):
    sink = JsonlEventSink(str(tmp_path / "events.jsonl"))
    attempts = 0

    def append(_path, _line):
        nonlocal attempts
        attempts += 1
        raise OSError(f"failure {attempts}")

    monkeypatch.setattr(event_log, "append_regular_text", append)
    asyncio.run(sink.emit(SimpleNamespace(type="first", data={})))
    asyncio.run(sink.emit(SimpleNamespace(type="second", data={})))

    assert sink.write_error == "OSError: failure 1"
    assert sink.dropped_events == 2
