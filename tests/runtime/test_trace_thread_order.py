"""Thread ordering guarantees for the asynchronous trace writer."""

from __future__ import annotations

import json
import threading

from opencollab.adapters.trace import Tracer


class _SlowPayload:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def __str__(self) -> str:
        self.started.set()
        assert self.release.wait(timeout=5)
        return "first"


def _records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_step_numbers_follow_queue_order_and_survive_reopen(tmp_path) -> None:
    tracer = Tracer("thread-order", output_dir=str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    first = threading.Thread(
        target=tracer.log_step,
        args=("test", {"value": _SlowPayload(started, release)}),
    )
    first.start()
    try:
        assert started.wait(timeout=5)
        tracer.log_step("test", {"value": "second"})
    finally:
        release.set()
        first.join(timeout=5)
    assert not first.is_alive()
    tracer.close()

    records = _records(tmp_path / "thread-order.jsonl")
    assert [(record["step"], record["payload"]["value"]) for record in records] == [
        (1, "second"),
        (2, "first"),
    ]

    reopened = Tracer("thread-order", output_dir=str(tmp_path))
    reopened.log_step("test", {"value": "third"})
    reopened.close()

    records = _records(tmp_path / "thread-order.jsonl")
    assert [(record["step"], record["payload"]["value"]) for record in records] == [
        (1, "second"),
        (2, "first"),
        (3, "third"),
    ]
    assert list(tmp_path.glob("thread-order.jsonl.corrupt-*")) == []


def test_close_racing_with_serialization_counts_the_dropped_step(tmp_path) -> None:
    tracer = Tracer("close-race", output_dir=str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    logging_thread = threading.Thread(
        target=tracer.log_step,
        args=("test", {"value": _SlowPayload(started, release)}),
    )
    logging_thread.start()
    assert started.wait(timeout=5)

    tracer.close()
    release.set()
    logging_thread.join(timeout=5)

    assert not logging_thread.is_alive()
    assert tracer.dropped_steps == 1
    assert _records(tmp_path / "close-race.jsonl") == []
