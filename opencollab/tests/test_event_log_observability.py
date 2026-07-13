from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

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


def test_jsonl_event_sink_remains_passive_when_parent_cannot_be_created(tmp_path):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("occupied", encoding="utf-8")
    sink = JsonlEventSink(str(blocker / "run.jsonl"))

    asyncio.run(sink.emit(SimpleNamespace(type="custom", data={"value": "ok"})))

    assert blocker.read_text(encoding="utf-8") == "occupied"
    assert sink.write_error is not None
    assert sink.dropped_events == 1
