from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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


def test_loop_monitor_truncate_obj_returns_json_safe_value():
    monitor = _load_loop_monitor_module()

    value = {"args": {"value": _CustomObject()}}
    result = monitor._truncate_obj(value)

    assert result == {"args": {"value": "custom-object"}}
    json.dumps(result)
